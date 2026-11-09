"""Upload existing files from the local uploads/ folder to S3-compatible storage.

Works with Backblaze B2, Cloudflare R2 or any S3-compatible provider. Object
keys stay identical to the existing DB file names, so no database changes are
needed. Run once after configuring the S3_* env vars (R2_* is also accepted):

    python migrate_to_s3.py [--skip-existing]
"""

import mimetypes
import os
import sys

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

ACCESS_KEY = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY_ID", "")
SECRET_KEY = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_ACCESS_KEY", "")
BUCKET = os.getenv("S3_BUCKET") or os.getenv("R2_BUCKET", "")
REGION = os.getenv("S3_REGION", "")
ENDPOINT = os.getenv("S3_ENDPOINT") or os.getenv("R2_ENDPOINT", "")

if not ENDPOINT:
    ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
    if ACCOUNT_ID:
        ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"

if not REGION:
    REGION = "auto"


def main():
    missing = [
        name
        for name, value in (
            ("S3_ACCESS_KEY_ID", ACCESS_KEY),
            ("S3_SECRET_ACCESS_KEY", SECRET_KEY),
            ("S3_BUCKET", BUCKET),
            ("S3_ENDPOINT/R2_ACCOUNT_ID", ENDPOINT),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing env vars: " + ", ".join(missing))

    if not os.path.isdir(UPLOAD_DIR):
        raise SystemExit(f"Uploads folder not found: {UPLOAD_DIR}")

    skip_existing = "--skip-existing" in sys.argv

    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": os.getenv("S3_ADDRESSING_STYLE", "path")},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )

    transfer_config = TransferConfig(
        multipart_threshold=100 * 1024 * 1024,
        multipart_chunksize=100 * 1024 * 1024,
        max_concurrency=4,
    )

    files = [f for f in os.listdir(UPLOAD_DIR) if os.path.isfile(os.path.join(UPLOAD_DIR, f))]
    if not files:
        print("No files to migrate.")
        return

    uploaded = 0
    skipped = 0
    for name in files:
        path = os.path.join(UPLOAD_DIR, name)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

        if skip_existing:
            try:
                client.head_object(Bucket=BUCKET, Key=name)
                skipped += 1
                print(f"Skipped (already exists): {name}")
                continue
            except Exception:
                pass

        try:
            client.upload_file(
                path,
                BUCKET,
                name,
                ExtraArgs={"ContentType": content_type},
                Config=transfer_config,
            )
            uploaded += 1
            print(f"Uploaded: {name}")
        except Exception as exc:
            skipped += 1
            print(f"Failed: {name} -> {exc}")

    print(f"\nDone. Uploaded={uploaded}, Failed/Skipped={skipped}, Total={len(files)}")


if __name__ == "__main__":
    main()
