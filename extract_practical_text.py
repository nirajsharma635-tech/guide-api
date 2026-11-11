"""Extract text from existing practical PDFs into the new text columns.

One-time backfill: har practical ka manual/solution PDF (local uploads/ ya
S3-compatible storage) padh kar pypdf se text nikalta hai aur
practicals.manual_text / practicals.solution_text mein save karta hai.

Usage:
    python extract_practical_text.py               # sirf empty columns fill karega
    python extract_practical_text.py --dry-run    # sirf report, kuch save nahi
    python extract_practical_text.py --overwrite   # existing text bhi replace

NOTE: sirf text-based PDFs se kaam karta hai. Scanned/photo PDFs me text
layer nahi hota, wo report me "no text" dikhenge aur skip ho jayenge.
"""

import argparse
import io
import os
import re
import sys

import httpx
from dotenv import load_dotenv
from pypdf import PdfReader
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

DATABASE_URL = os.getenv("DATABASE_URL", "")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

if (
    DATABASE_URL.startswith("postgresql+psycopg://")
    and "sslmode=" not in DATABASE_URL
    and "localhost" not in DATABASE_URL
    and "127.0.0.1" not in DATABASE_URL
):
    separator = "&" if "?" in DATABASE_URL else "?"
    DATABASE_URL = f"{DATABASE_URL}{separator}sslmode=require"

S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.getenv("S3_BUCKET") or os.getenv("R2_BUCKET", "")
S3_REGION = os.getenv("S3_REGION", "")
S3_ENDPOINT = os.getenv("S3_ENDPOINT") or os.getenv("R2_ENDPOINT", "")

if not S3_ENDPOINT:
    _r2_account_id = os.getenv("R2_ACCOUNT_ID", "")
    if _r2_account_id:
        S3_ENDPOINT = f"https://{_r2_account_id}.r2.cloudflarestorage.com"

if not S3_REGION:
    S3_REGION = "auto"

S3_ENABLED = bool(S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY and S3_BUCKET and S3_ENDPOINT)

MIN_TEXT_CHARS = 40

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        import boto3
        from botocore.config import Config as BotoConfig

        config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        )
        _s3_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY_ID,
            aws_secret_access_key=S3_SECRET_ACCESS_KEY,
            region_name=S3_REGION,
            config=config,
        )
    return _s3_client


def fetch_pdf_bytes(key: str) -> bytes:
    if not key:
        raise RuntimeError("empty PDF key")

    if re.match(r"^https?://", key, re.IGNORECASE):
        response = httpx.get(key, follow_redirects=True, timeout=90)
        response.raise_for_status()
        return response.content

    local_path = os.path.join(UPLOAD_DIR, key.lstrip("/"))
    if os.path.isfile(local_path):
        with open(local_path, "rb") as handle:
            return handle.read()

    if S3_ENABLED:
        obj = get_s3_client().get_object(Bucket=S3_BUCKET, Key=key.lstrip("/"))
        return obj["Body"].read()

    raise RuntimeError("PDF neither in local uploads/ nor is storage configured")


def extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        page_text = re.sub(r"[ \t]+", " ", page_text)
        page_text = re.sub(r"\n{3,}", "\n\n", page_text)
        if page_text.strip():
            parts.append(page_text.strip())
    return "\n\n".join(parts).strip()


def save_text(session, practical_id: int, column: str, value: str) -> None:
    session.execute(
        sql_text(f'UPDATE practicals SET "{column}" = :value WHERE id = :pid'),
        {"value": value, "pid": practical_id},
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill practical PDF text into manual_text/solution_text"
    )
    parser.add_argument("--dry-run", action="store_true", help="sirf report, kuch save nahi")
    parser.add_argument("--overwrite", action="store_true", help="existing text replace karo")
    args = parser.parse_args()

    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is not set")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)

    stats = {"extracted": 0, "no_text": 0, "fetch_failed": 0, "no_pdf": 0, "already_set": 0}
    targets = (
        ("syllabus_pdf", "manual_text", "manual"),
        ("solution_pdf", "solution_text", "solution"),
    )

    with Session() as session:
        rows = session.execute(
            sql_text(
                "SELECT id, syllabus_title, syllabus_pdf, solution_pdf, "
                "manual_text, solution_text FROM practicals ORDER BY id"
            )
        ).mappings().all()

        if not rows:
            print("No practicals found in the database. Kuch karne ko nahi hai.")
            return

        print(f"Found {len(rows)} practicals. Mode: {'DRY-RUN' if args.dry_run else 'SAVE'}\n")

        for row in rows:
            for pdf_col, text_col, label in targets:
                pdf_key = row[pdf_col]
                current = row[text_col]

                if not pdf_key:
                    stats["no_pdf"] += 1
                    continue

                if current and not args.overwrite:
                    stats["already_set"] += 1
                    continue

                title = row["syllabus_title"] or f"practical #{row['id']}"
                prefix = f"[#{row['id']} {label}] {title}"

                try:
                    pdf_bytes = fetch_pdf_bytes(pdf_key)
                except Exception as exc:
                    print(f"FAIL (fetch): {prefix} -> {exc}")
                    stats["fetch_failed"] += 1
                    continue

                try:
                    extracted = extract_pdf_text(pdf_bytes)
                except Exception as exc:
                    print(f"FAIL (parse): {prefix} -> {exc}")
                    stats["fetch_failed"] += 1
                    continue

                if len(extracted) < MIN_TEXT_CHARS:
                    print(f"NO TEXT: {prefix} (PDF scanned ya text-less hai, {len(extracted)} chars)")
                    stats["no_text"] += 1
                    continue

                if args.dry_run:
                    print(f"WOULD SAVE: {prefix} ({len(extracted)} chars)")
                else:
                    save_text(session, row["id"], text_col, extracted)
                    print(f"SAVED: {prefix} ({len(extracted)} chars)")
                stats["extracted"] += 1

        if not args.dry_run:
            session.commit()

    print("\n===== SUMMARY =====")
    print(f"Text extracted : {stats['extracted']}")
    print(f"No text (scanned): {stats['no_text']}")
    print(f"Fetch/parse fail: {stats['fetch_failed']}")
    print(f"PDF missing    : {stats['no_pdf']}")
    print(f"Already set     : {stats['already_set']}")

    if args.dry_run:
        print("\nDry-run tha — kuch save nahi hua. Real run ke liye --dry-run hata ke chalayein.")
    else:
        print("\nDone! Ab admin panel ya app me text content check kar sakte ho.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
