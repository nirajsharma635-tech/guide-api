import hashlib
import hmac
import os
import random
import re
import secrets
import shutil
import smtplib
import string
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Annotated, Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, EmailStr, Field
from pwdlib import PasswordHash
from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

load_dotenv()

app = FastAPI(title="Guide API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./college.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False)

password_hash = PasswordHash.recommended()
security = HTTPBearer()

SECRET_KEY = os.getenv("SECRET_KEY", "local-dev-only-change-before-deployment")

OTP_TTL_MINUTES = 5
OTP_VERIFIED_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    course: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    class_field: Mapped[Optional[str]] = mapped_column("class", String(50), nullable=True)
    referred_by: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    wallet_balance: Mapped[Optional[float]] = mapped_column(Float, default=0, nullable=True)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    logo: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    company_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=1)


class SubCategory(Base):
    __tablename__ = "subcategories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, index=True)
    class_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(150))


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    instructor: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    image: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0)
    members: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    is_best_seller: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    progress: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=0)


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcat_id: Mapped[int] = mapped_column(Integer, index=True)
    bookname: Mapped[str] = mapped_column(String(200))
    referencename: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    image: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcat_id: Mapped[int] = mapped_column(Integer, index=True)
    chapter_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chapter_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    image: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class QuestionPaper(Base):
    __tablename__ = "question_papers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcat_id: Mapped[int] = mapped_column(Integer, index=True)
    tag: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    image: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class QuestionBankItem(Base):
    __tablename__ = "question_bank"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcat_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200))
    pdf: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Practical(Base):
    __tablename__ = "practicals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcat_id: Mapped[int] = mapped_column(Integer, index=True)
    syllabus_title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    syllabusPdf: Mapped[Optional[str]] = mapped_column("syllabus_pdf", String(255), nullable=True)
    solutionPdf: Mapped[Optional[str]] = mapped_column("solution_pdf", String(255), nullable=True)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    class_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    topic: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    lang: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class SyllabusTopic(Base):
    __tablename__ = "syllabus_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcat_id: Mapped[int] = mapped_column(Integer, index=True)
    unit: Mapped[int] = mapped_column(Integer, default=1)
    topic: Mapped[str] = mapped_column(String(300))


class SyllabusStatus(Base):
    __tablename__ = "syllabus_status"
    __table_args__ = (UniqueConstraint("user_id", "topic_id", name="uq_syllabus_user_topic"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    topic_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(30), default="Pending")


class NewsItem(Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProjectCategory(Base):
    __tablename__ = "project_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    icon: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class ProjectSubCategory(Base):
    __tablename__ = "project_subcategories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    information: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    advantages: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    disadvantages: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ForumQuestion(Base):
    __tablename__ = "forum_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ForumReply(Base):
    __tablename__ = "forum_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RechargeTransaction(Base):
    __tablename__ = "recharge_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    payer_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    transaction_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    usertrant_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="Pending")
    reject_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    transaction_type: Mapped[str] = mapped_column(String(20), default="Debit")
    amount: Mapped[float] = mapped_column(Float, default=0)
    purchase: Mapped[Optional[str]] = mapped_column("purchase", String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="Completed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EmailOtp(Base):
    __tablename__ = "email_otps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    otp_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(bind=engine)


def ensure_user_columns():
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns("users")}
    additions = {
        "user_id": "VARCHAR(50)",
        "phone": "VARCHAR(20)",
        "course": "VARCHAR(100)",
        "gender": "VARCHAR(20)",
        "class": "VARCHAR(50)",
        "referred_by": "VARCHAR(50)",
        "wallet_balance": "FLOAT DEFAULT 0",
    }

    with engine.begin() as connection:
        for column, ddl_type in additions.items():
            if column not in existing:
                connection.execute(
                    text(f'ALTER TABLE users ADD COLUMN "{column}" {ddl_type}')
                )


try:
    ensure_user_columns()
except Exception as exc:
    print(f"Warning: could not migrate users table automatically: {exc}")


def seed_defaults():
    db = SessionLocal()
    try:
        if db.query(Category).count() == 0:
            db.add_all([
                Category(id=1, name="Syllabus", logo="book", company_id=1),
                Category(id=2, name="Notes", logo="document", company_id=1),
                Category(id=3, name="Question Bank", logo="help-circle", company_id=1),
                Category(id=4, name="Books", logo="folder", company_id=1),
                Category(id=5, name="Question Papers", logo="clipboard", company_id=1),
                Category(id=6, name="Videos", logo="videocam", company_id=1),
                Category(id=7, name="Practical", logo="flask", company_id=1),
                Category(id=8, name="Projects", logo="rocket", company_id=1),
            ])

        if db.query(SubCategory).count() == 0:
            subject_map = {
                1: [
                    "Imperative Programming",
                    "Digital Electronics",
                    "Operating Systems",
                    "Discrete Mathematics",
                    "Communication Skills",
                ],
                2: [
                    "Object Oriented Programming",
                    "Data Structures",
                    "Database Systems",
                    "Computer Networks",
                    "Applied Mathematics",
                ],
                3: [
                    "Software Engineering",
                    "Web Programming",
                    "Network Security",
                    "Mobile Computing",
                    "Project Work",
                ],
            }
            for class_level, subjects in subject_map.items():
                for subject in subjects:
                    db.add(SubCategory(category_id=1, class_level=class_level, name=subject))

        db.commit()
    finally:
        db.close()


try:
    seed_defaults()
except Exception as exc:
    print(f"Warning: could not seed default content: {exc}")


class RegisterInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    phone: Optional[str] = None
    course: Optional[str] = None
    gender: Optional[str] = None
    class_field: Optional[str] = Field(default=None, alias="class")
    user_id: Optional[str] = None
    referral_code: Optional[str] = Field(default=None, alias="referralCode")

    class Config:
        populate_by_name = True


class LoginInput(BaseModel):
    email: EmailStr
    password: str


class OtpSendInput(BaseModel):
    email: EmailStr


class OtpVerifyInput(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=4, max_length=10)


class ReferralInput(BaseModel):
    referralCode: Optional[str] = None
    code: Optional[str] = None


class ChangePasswordInput(BaseModel):
    email: EmailStr
    new_password: str = Field(min_length=6, max_length=128)


class UpdateProfileInput(BaseModel):
    user_id: str
    field: str
    value: str


class DeleteAccountInput(BaseModel):
    user_id: str


class ForumQuestionInput(BaseModel):
    name: str
    text: str
    user_id: Optional[str] = None


class ForumReplyInput(BaseModel):
    question_id: int
    name: str
    text: str


class WalletTransactionInput(BaseModel):
    user_id: Optional[str] = None
    transaction_type: str = "Debit"
    amount: float = 0
    Purchase: Optional[str] = None


class RechargeInput(BaseModel):
    user_id: Optional[str] = None
    payer_name: Optional[str] = None
    amount: float = 0
    transaction_id: Optional[str] = None
    usertrant_id: Optional[str] = None


class NoteUploadInput(BaseModel):
    chapter_num: Optional[int] = None
    chapter_name: Optional[str] = None
    image: Optional[str] = None
    subcat_id: Optional[int] = None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def normalize_email(email: str) -> str:
    return str(email).lower().strip()


def create_token(user: User):
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    return jwt.encode(
        {"sub": str(user.id), "exp": expires},
        SECRET_KEY,
        algorithm="HS256",
    )


def user_payload(user: User) -> dict:
    return {
        "id": user.id,
        "user_id": user.user_id or str(user.id),
        "name": user.name,
        "email": user.email,
        "phone": user.phone,
        "course": user.course,
        "gender": user.gender,
        "class": user.class_field,
        "wallet_balance": user.wallet_balance or 0,
    }


def find_user(db: Session, user_ref: str) -> Optional[User]:
    user_ref = str(user_ref).strip()
    if not user_ref:
        return None

    user = db.query(User).filter(User.user_id == user_ref).first()
    if user:
        return user

    if user_ref.isdigit():
        return db.get(User, int(user_ref))

    return None


def fmt_datetime(value: Optional[datetime]) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def is_truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def otp_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def send_verification_email(recipient: str, code: str):
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    use_ssl = is_truthy(os.getenv("SMTP_USE_SSL", "false"))
    use_tls = is_truthy(os.getenv("SMTP_USE_TLS", "true"))

    if not host or not sender:
        raise RuntimeError("SMTP is not configured")

    text_body = (
        f"Your Guide verification code is {code}.\n\n"
        f"This code expires in {OTP_TTL_MINUTES} minutes.\n"
        "Never share this code with anyone.\n\n"
        "If you did not request this code, you can ignore this email."
    )
    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#eef2ff;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef2ff;">
    <tr>
      <td align="center" style="padding:36px 16px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border-radius:24px;overflow:hidden;box-shadow:0 24px 50px -28px rgba(15,23,42,0.45);">
          <tr>
            <td style="background:linear-gradient(135deg,#6366f1 0%,#4f46e5 55%,#3b82f6 100%);padding:30px 28px;text-align:center;">
              <div style="display:inline-block;width:52px;height:52px;line-height:52px;border-radius:16px;background:rgba(255,255,255,0.18);color:#ffffff;font-size:26px;font-weight:800;">G</div>
              <h1 style="margin:14px 0 0;color:#ffffff;font-size:22px;font-weight:800;letter-spacing:-0.3px;">Verify your email</h1>
              <p style="margin:6px 0 0;color:rgba(255,255,255,0.8);font-size:13px;">Guide &middot; Learn smarter</p>
            </td>
          </tr>
          <tr>
            <td style="padding:32px 28px 8px;">
              <p style="margin:0 0 10px;color:#0f172a;font-size:15px;font-weight:700;">Hi there,</p>
              <p style="margin:0 0 22px;color:#475569;font-size:14px;line-height:1.6;">Use the verification code below to finish creating your Guide account.</p>
              <div style="padding:20px 12px;background:#f5f7ff;border:1.5px dashed #c7d2fe;border-radius:18px;text-align:center;">
                <span style="color:#4f46e5;font-size:34px;font-weight:800;letter-spacing:11px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{code}</span>
              </div>
              <p style="margin:18px 0 0;color:#64748b;font-size:13px;line-height:1.6;">This code expires in <strong style="color:#334155;">{OTP_TTL_MINUTES} minutes</strong>. Never share it with anyone.</p>
              <p style="margin:10px 0 0;color:#94a3b8;font-size:12px;line-height:1.6;">If you did not request this code, you can safely ignore this email.</p>
            </td>
          </tr>
          <tr>
            <td style="padding:22px 28px 26px;">
              <div style="height:1px;background:#e8edf5;margin-bottom:18px;"></div>
              <p style="margin:0;color:#94a3b8;font-size:11.5px;line-height:1.6;text-align:center;">This is an automated message from Guide. Please do not reply to this email.</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    message = EmailMessage()
    message["Subject"] = "Your Guide verification code"
    message["From"] = f"Guide <{sender}>"
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20) as server:
            if username:
                server.login(username, password)
            server.send_message(message)
        return

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        if username:
            server.login(username, password)
        server.send_message(message)


def save_upload(upload: UploadFile, allowed_extensions: set) -> str:
    original = os.path.basename(upload.filename or "file")
    extension = os.path.splitext(original)[1].lower()
    if extension not in allowed_extensions:
        raise HTTPException(status_code=400, detail="This file type is not allowed")

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original)
    filename = f"{int(datetime.now().timestamp() * 1000)}_{secrets.token_hex(4)}_{safe_name}"
    destination = os.path.join(UPLOAD_DIR, filename)

    with open(destination, "wb") as output:
        shutil.copyfileobj(upload.file, output)

    return filename


@app.get("/")
def home():
    return {"status": "success", "message": "Guide API is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", status_code=201)
def register(data: RegisterInput, db: Annotated[Session, Depends(get_db)]):
    email = normalize_email(data.email)

    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already exists.")

    otp_record = db.query(EmailOtp).filter(EmailOtp.email == email).first()
    if not otp_record or otp_record.verified_at is None:
        raise HTTPException(status_code=403, detail="Email verification required.")

    if otp_record.expires_at < datetime.utcnow():
        db.delete(otp_record)
        db.commit()
        raise HTTPException(status_code=403, detail="Email verification expired. Request a new OTP.")

    referred_by = None
    if data.referral_code:
        referrer = find_user(db, data.referral_code)
        if referrer:
            referred_by = referrer.user_id or str(referrer.id)

    user = User(
        name=data.name.strip(),
        email=email,
        hashed_password=password_hash.hash(data.password),
        phone=data.phone.strip() if data.phone else None,
        course=data.course.strip() if data.course else None,
        gender=data.gender.strip() if data.gender else None,
        class_field=data.class_field.strip() if data.class_field else None,
        user_id=data.user_id.strip() if data.user_id else None,
        referred_by=referred_by,
        wallet_balance=0,
    )
    db.add(user)
    db.delete(otp_record)
    db.commit()
    db.refresh(user)

    return {
        "success": True,
        "message": "Registration successful",
        "user_id": user.user_id or str(user.id),
        "user": user_payload(user),
    }


@app.post("/login")
def login(data: LoginInput, db: Annotated[Session, Depends(get_db)]):
    email = normalize_email(data.email)
    user = db.query(User).filter(User.email == email).first()

    if not user or not password_hash.verify(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "status": "success",
        "message": "Login successful",
        "access_token": create_token(user),
        "token_type": "bearer",
        "user": user_payload(user),
    }


@app.get("/me")
def me(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user_payload(user)


@app.post("/otp/send")
def send_otp(data: OtpSendInput, db: Annotated[Session, Depends(get_db)] = None):
    email = normalize_email(data.email)
    code = "".join(random.choices(string.digits, k=5))
    now = datetime.utcnow()
    expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)

    record = db.query(EmailOtp).filter(EmailOtp.email == email).first()
    if record:
        record.otp_hash = otp_hash(code)
        record.attempts = 0
        record.verified_at = None
        record.expires_at = expires_at
        record.created_at = now
    else:
        db.add(EmailOtp(
            email=email,
            otp_hash=otp_hash(code),
            expires_at=expires_at,
        ))

    smtp_configured = bool(
        os.getenv("SMTP_HOST", "").strip()
        and (os.getenv("SMTP_FROM", "").strip() or os.getenv("SMTP_USER", "").strip())
    )
    dev_mode = is_truthy(os.getenv("OTP_DEV_MODE", "false")) or not smtp_configured
    try:
        send_verification_email(email, code)
    except Exception as exc:
        if dev_mode:
            print(f"[DEV OTP] {email} -> {code} ({exc})")
        else:
            db.rollback()
            return {
                "status": "error",
                "message": "Verification email could not be sent. Please try again later.",
            }

    db.commit()

    response = {
        "status": "success",
        "message": "OTP sent successfully",
        "expires_in": OTP_TTL_MINUTES * 60,
    }
    if dev_mode:
        response["otp"] = code

    return response


@app.post("/otp/verify")
def verify_otp(data: OtpVerifyInput, db: Annotated[Session, Depends(get_db)] = None):
    email = normalize_email(data.email)
    record = db.query(EmailOtp).filter(EmailOtp.email == email).first()

    if not record:
        return {"status": "error", "message": "Please request a new OTP."}

    now = datetime.utcnow()
    if record.expires_at < now:
        db.delete(record)
        db.commit()
        return {"status": "error", "message": "OTP expired. Please request a new one."}

    if record.attempts >= OTP_MAX_ATTEMPTS:
        db.delete(record)
        db.commit()
        return {"status": "error", "message": "Too many invalid attempts. Please request a new OTP."}

    record.attempts += 1
    if not hmac.compare_digest(record.otp_hash, otp_hash(data.otp.strip())):
        db.commit()
        return {"status": "error", "message": "Invalid OTP."}

    record.verified_at = now
    record.expires_at = now + timedelta(minutes=OTP_VERIFIED_TTL_MINUTES)
    db.commit()
    return {"status": "success", "message": "OTP verified successfully"}


@app.post("/check-referral")
def check_referral(data: ReferralInput, db: Annotated[Session, Depends(get_db)]):
    code = (data.referralCode or data.code or "").strip()

    if not code:
        return {"valid": False, "message": "Referral code is required."}

    user = find_user(db, code)
    if user:
        return {"valid": True, "message": "Referral code is valid."}

    return {"valid": False, "message": "Invalid referral code."}


@app.post("/change-password")
def change_password(data: ChangePasswordInput, db: Annotated[Session, Depends(get_db)]):
    email = normalize_email(data.email)
    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(status_code=404, detail="Email is not registered")

    user.hashed_password = password_hash.hash(data.new_password)
    db.commit()

    return {"status": "success", "message": "Password updated successfully"}


@app.post("/update-profile")
def update_profile(data: UpdateProfileInput, db: Annotated[Session, Depends(get_db)]):
    user = find_user(db, data.user_id)

    if not user:
        return {"success": False, "error": "User not found"}

    allowed_fields = {
        "name": "name",
        "phone": "phone",
        "gender": "gender",
        "course": "course",
        "class": "class_field",
    }

    if data.field not in allowed_fields:
        return {"success": False, "error": "This field cannot be updated"}

    setattr(user, allowed_fields[data.field], data.value.strip())
    db.commit()
    db.refresh(user)

    return {"success": True, "field": data.field, "value": data.value.strip()}


@app.post("/delete-account")
def delete_account(data: DeleteAccountInput, db: Annotated[Session, Depends(get_db)]):
    user = find_user(db, data.user_id)

    if not user:
        return {"success": False, "error": "User not found"}

    db.delete(user)
    db.commit()

    return {"success": "Account deleted successfully"}


@app.get("/categories")
def get_categories(
    company_id: int = 1,
    db: Annotated[Session, Depends(get_db)] = None,
):
    rows = db.query(Category).filter(Category.company_id == company_id).order_by(Category.id).all()
    return {
        "status": "success",
        "data": [{"id": row.id, "name": row.name, "logo": row.logo} for row in rows],
    }


@app.get("/subcategories")
def get_subcategories(
    class_level: Annotated[int, Query(alias="class")] = 1,
    category_id: int = 1,
    db: Annotated[Session, Depends(get_db)] = None,
):
    rows = (
        db.query(SubCategory)
        .filter(SubCategory.category_id == category_id, SubCategory.class_level == class_level)
        .order_by(SubCategory.id)
        .all()
    )
    return {
        "status": "success",
        "data": [{"id": row.id, "name": row.name} for row in rows],
    }


@app.get("/courses")
def get_courses(db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(Course).order_by(Course.id).all()
    data = [
        {
            "id": row.id,
            "title": row.title,
            "instructor": row.instructor,
            "image": row.image,
            "price": row.price or 0,
            "members": row.members or 0,
            "isBestSeller": row.is_best_seller or 0,
            "category": row.category,
            "progress": row.progress or 0,
        }
        for row in rows
    ]
    return {"status": "success", "data": data}


@app.get("/books")
def get_books(subcat_id: int, db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(Book).filter(Book.subcat_id == subcat_id).order_by(Book.id).all()
    return {
        "status": "success",
        "data": [
            {
                "id": row.id,
                "subcat_id": row.subcat_id,
                "bookname": row.bookname,
                "referencename": row.referencename,
                "image": row.image,
            }
            for row in rows
        ],
    }


@app.get("/notes")
def get_notes(subcat_id: int, db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(Note).filter(Note.subcat_id == subcat_id).order_by(Note.id).all()
    return {
        "status": "success",
        "data": [
            {
                "id": row.id,
                "subcat_id": row.subcat_id,
                "chapter_num": row.chapter_num,
                "chapter_name": row.chapter_name,
                "image": row.image,
            }
            for row in rows
        ],
    }


@app.get("/practicals")
def get_practicals(subcat_id: int, db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(Practical).filter(Practical.subcat_id == subcat_id).order_by(Practical.id).all()
    return {
        "status": "success",
        "data": [
            {
                "id": row.id,
                "subcat_id": row.subcat_id,
                "syllabus_title": row.syllabus_title,
                "syllabusPdf": row.syllabusPdf,
                "solutionPdf": row.solutionPdf,
            }
            for row in rows
        ],
    }


@app.get("/question-papers")
def get_question_papers(subcat_id: int, db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(QuestionPaper).filter(QuestionPaper.subcat_id == subcat_id).order_by(QuestionPaper.id).all()
    return [
        {
            "id": row.id,
            "subcat_id": row.subcat_id,
            "tag": row.tag,
            "title": row.title,
            "image": row.image,
        }
        for row in rows
    ]


@app.get("/question-bank")
def get_question_bank(subcat_id: int, db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(QuestionBankItem).filter(QuestionBankItem.subcat_id == subcat_id).order_by(QuestionBankItem.id).all()
    return [
        {
            "id": row.id,
            "subcat_id": row.subcat_id,
            "title": row.title,
            "pdf": row.pdf,
        }
        for row in rows
    ]


@app.get("/videos")
def get_videos(
    class_level: Annotated[int, Query(alias="class")] = 1,
    db: Annotated[Session, Depends(get_db)] = None,
):
    rows = db.query(Video).filter(Video.class_level == class_level).order_by(Video.id).all()
    return {
        "videos": [
            {
                "id": row.id,
                "topic": row.topic,
                "subject": row.subject,
                "lang": row.lang,
                "url": row.url,
            }
            for row in rows
        ]
    }


@app.get("/syllabus/topics")
def get_syllabus_topics(
    subcat_id: int,
    user_id: Optional[str] = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    topics = db.query(SyllabusTopic).filter(SyllabusTopic.subcat_id == subcat_id).order_by(SyllabusTopic.unit, SyllabusTopic.id).all()

    status_map = {}
    if user_id:
        status_rows = db.query(SyllabusStatus).filter(SyllabusStatus.user_id == user_id).all()
        status_map = {row.topic_id: row.status for row in status_rows}

    result = {f"unit_{unit}": [] for unit in range(1, 6)}
    for topic in topics:
        key = f"unit_{topic.unit}"
        if key not in result:
            continue
        result[key].append({
            "id": topic.id,
            "Unit": topic.unit,
            "subcat_id": topic.subcat_id,
            "Topic": topic.topic,
            "Status": status_map.get(topic.id, "Pending"),
        })

    return result


@app.get("/syllabus/status")
def set_syllabus_status(
    subcat_id: int,
    status: str,
    user_id: str,
    db: Annotated[Session, Depends(get_db)] = None,
):
    record = (
        db.query(SyllabusStatus)
        .filter(SyllabusStatus.user_id == user_id, SyllabusStatus.topic_id == subcat_id)
        .first()
    )
    if record:
        record.status = status
    else:
        db.add(SyllabusStatus(user_id=user_id, topic_id=subcat_id, status=status))
    db.commit()
    return {"status": True, "message": "Status updated successfully"}


@app.get("/news")
def get_news(db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(NewsItem).order_by(NewsItem.created_at.desc(), NewsItem.id.desc()).all()
    return {
        "status": "success",
        "data": [
            {
                "id": row.id,
                "title": row.title,
                "description": row.description or "",
                "image": row.image or "",
                "author": row.author or "",
                "Date": fmt_datetime(row.created_at),
            }
            for row in rows
        ],
    }


@app.get("/project-categories")
def get_project_categories(db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(ProjectCategory).order_by(ProjectCategory.id).all()
    return [{"id": row.id, "name": row.name, "icon": row.icon} for row in rows]


@app.get("/project-subcategories")
def get_project_subcategories(category_id: int, db: Annotated[Session, Depends(get_db)] = None):
    rows = (
        db.query(ProjectSubCategory)
        .filter(ProjectSubCategory.category_id == category_id)
        .order_by(ProjectSubCategory.id)
        .all()
    )
    return [
        {
            "id": row.id,
            "category_id": row.category_id,
            "name": row.name,
            "information": row.information or "",
            "advantages": row.advantages or "",
            "disadvantages": row.disadvantages or "",
        }
        for row in rows
    ]


@app.get("/forum/questions")
def get_forum_questions(db: Annotated[Session, Depends(get_db)] = None):
    questions = db.query(ForumQuestion).order_by(ForumQuestion.id.desc()).all()
    replies = db.query(ForumReply).order_by(ForumReply.id).all()
    reply_map = {}
    for reply in replies:
        reply_map.setdefault(reply.question_id, []).append({
            "id": reply.id,
            "name": reply.name,
            "text": reply.text,
            "created_at": fmt_datetime(reply.created_at),
        })

    return {
        "questions": [
            {
                "id": row.id,
                "user_id": row.user_id,
                "name": row.name,
                "text": row.text,
                "created_at": fmt_datetime(row.created_at),
                "replies": reply_map.get(row.id, []),
            }
            for row in questions
        ]
    }


@app.post("/forum/questions")
def post_forum_question(data: ForumQuestionInput, db: Annotated[Session, Depends(get_db)]):
    if not data.text.strip():
        return {"success": False, "error": "Question cannot be empty"}

    db.add(ForumQuestion(user_id=data.user_id, name=data.name, text=data.text.strip()))
    db.commit()
    return {"success": True, "message": "Question posted successfully"}


@app.post("/forum/replies")
def post_forum_reply(data: ForumReplyInput, db: Annotated[Session, Depends(get_db)]):
    question = db.get(ForumQuestion, data.question_id)
    if not question:
        return {"success": False, "error": "Question not found"}

    if not data.text or not data.text.strip():
        return {"success": False, "error": "Reply cannot be empty"}

    db.add(ForumReply(question_id=data.question_id, name=data.name, text=data.text.strip()))
    db.commit()
    return {"success": True, "message": "Reply posted successfully"}


@app.get("/wallet")
def get_wallet(user_id: str, db: Annotated[Session, Depends(get_db)] = None):
    user = find_user(db, user_id)
    if not user:
        return {"balance": 0, "error": "User not found"}
    return {"balance": user.wallet_balance or 0}


@app.post("/wallet")
def update_wallet(
    data: WalletTransactionInput,
    user_id: Optional[str] = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    reference = (data.user_id or user_id or "").strip()
    user = find_user(db, reference)
    if not user:
        return {"error": "User not found"}

    amount = float(data.amount or 0)
    if amount <= 0:
        return {"error": "Invalid amount"}

    balance = float(user.wallet_balance or 0)
    if data.transaction_type.lower() == "debit":
        if balance < amount:
            return {"error": "Insufficient balance"}
        balance -= amount
    else:
        balance += amount

    user.wallet_balance = balance
    db.add(WalletTransaction(
        user_id=user.user_id or str(user.id),
        transaction_type=data.transaction_type,
        amount=amount,
        purchase=data.Purchase,
        status="Completed",
    ))
    db.commit()

    return {"message": "Transaction successful", "new_balance": balance}


@app.get("/wallet/recharges")
def get_recharges(user_id: str, db: Annotated[Session, Depends(get_db)] = None):
    rows = (
        db.query(RechargeTransaction)
        .filter(RechargeTransaction.user_id == user_id)
        .order_by(RechargeTransaction.id.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "payer_name": row.payer_name,
            "amount": row.amount,
            "transaction_id": row.transaction_id,
            "usertrant_id": row.usertrant_id,
            "status": row.status,
            "reject_reason": row.reject_reason,
            "created_at": fmt_datetime(row.created_at),
        }
        for row in rows
    ]


@app.get("/wallet/transactions")
def get_wallet_transactions(
    user_id: str,
    transactions: bool = True,
    db: Annotated[Session, Depends(get_db)] = None,
):
    rows = (
        db.query(WalletTransaction)
        .filter(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.id.desc())
        .all()
    )
    if not rows:
        return {"message": "No transactions found", "transactions": []}

    return {
        "transactions": [
            {
                "id": row.id,
                "transaction_type": row.transaction_type,
                "amount": row.amount,
                "Purchase": row.purchase,
                "status": row.status,
                "created_at": fmt_datetime(row.created_at),
            }
            for row in rows
        ]
    }


@app.post("/wallet/recharge")
def store_recharge(
    data: RechargeInput,
    user_id: Optional[str] = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    reference = (data.user_id or user_id or "").strip()
    user = find_user(db, reference)
    if not user:
        return {"success": False, "message": "User not found"}

    db.add(RechargeTransaction(
        user_id=user.user_id or str(user.id),
        payer_name=data.payer_name,
        amount=float(data.amount or 0),
        transaction_id=data.transaction_id,
        usertrant_id=data.usertrant_id,
        status="Pending",
    ))
    db.commit()

    return {
        "success": True,
        "message": "Transaction submitted successfully. Your wallet will be updated shortly.",
    }


@app.get("/semesters")
def get_semesters():
    return [{"Semester": semester} for semester in range(1, 7)]


@app.get("/subjects")
def get_subjects(semester: int, db: Annotated[Session, Depends(get_db)] = None):
    class_level = (semester + 1) // 2
    rows = (
        db.query(SubCategory)
        .filter(SubCategory.class_level == class_level)
        .order_by(SubCategory.id)
        .all()
    )
    return [{"id": row.id, "name": row.name} for row in rows]


@app.post("/upload/notes")
def upload_notes_file(
    file: Annotated[UploadFile, File(alias="myFile")],
):
    filename = save_upload(file, {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"})
    return {"status": "success", "file_name": filename}


@app.post("/upload/image")
def upload_image(
    file: Annotated[UploadFile, File()],
):
    filename = save_upload(file, {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"})
    return {"status": "success", "file": {"fileName": filename}}


@app.post("/notes/upload")
def save_note_data(data: NoteUploadInput, db: Annotated[Session, Depends(get_db)]):
    if not data.subcat_id:
        return {"status": "error", "message": "Subject is required"}

    db.add(Note(
        subcat_id=data.subcat_id,
        chapter_num=data.chapter_num,
        chapter_name=data.chapter_name,
        image=data.image,
    ))
    db.commit()
    return {"status": "success", "message": "Notes uploaded successfully"}


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
