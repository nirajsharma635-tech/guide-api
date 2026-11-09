import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import random
import re
import secrets
import shutil
import smtplib
import string
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Annotated, List, Optional

import jwt
import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, EmailStr, Field
from pwdlib import PasswordHash
from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint, and_, create_engine, func, inspect, or_, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

try:
    import boto3
    from botocore.config import Config as BotoConfig
except ImportError:  # object storage optional; local disk fallback rahega
    boto3 = None
    BotoConfig = None

load_dotenv()

app = FastAPI(title="Guide API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8100", "*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
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

YOUPI_WEBHOOK_SECRET = os.getenv("YOUPI_WEBHOOK_SECRET", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

OTP_TTL_MINUTES = 5
OTP_VERIFIED_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- S3-compatible object storage (Backblaze B2 / Cloudflare R2 / any S3) ---
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("R2_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("R2_SECRET_ACCESS_KEY", "")
S3_BUCKET = os.getenv("S3_BUCKET") or os.getenv("R2_BUCKET", "")
S3_REGION = os.getenv("S3_REGION", "")
S3_ENDPOINT = os.getenv("S3_ENDPOINT") or os.getenv("R2_ENDPOINT", "")

if not S3_ENDPOINT:
    _r2_account_id = os.getenv("R2_ACCOUNT_ID", "")
    if _r2_account_id:
        S3_ENDPOINT = f"https://{_r2_account_id}.r2.cloudflarestorage.com"

# R2 uses "auto"; Backblaze B2 ke liye region zaroori hai (e.g. us-west-004).
if not S3_REGION:
    S3_REGION = "auto"

# B2 aur zyadatar S3 providers ke liye path-style addressing sabse reliable hai.
S3_ADDRESSING_STYLE = os.getenv("S3_ADDRESSING_STYLE", "path")

S3_ENABLED = bool(
    boto3 and S3_ENDPOINT and S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY and S3_BUCKET
)
S3_URL_TTL_SECONDS = int(
    os.getenv("S3_URL_TTL_SECONDS") or os.getenv("R2_URL_TTL_SECONDS") or "900"
)

_s3_client = None


def get_s3_client():
    global _s3_client
    if _s3_client is None:
        config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": S3_ADDRESSING_STYLE},
            retries={"max_attempts": 5, "mode": "standard"},
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


# Backwards-compatible aliases
R2_ENABLED = S3_ENABLED
R2_BUCKET = S3_BUCKET
R2_URL_TTL_SECONDS = S3_URL_TTL_SECONDS
get_r2_client = get_s3_client


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
    role: Mapped[Optional[str]] = mapped_column(String(20), default="student", nullable=True, server_default="student")


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
    semester: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
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
    reference: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, unique=True, index=True)
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


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(Integer, index=True)
    unit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chapter_name: Mapped[str] = mapped_column(String(200))
    chapter_num: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[int] = mapped_column(Integer, index=True)
    unit_num: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))


class StudyMaterial(Base):
    __tablename__ = "study_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    chapter_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    unit_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200))
    material_type: Mapped[Optional[str]] = mapped_column(String(30), default="pdf")
    file_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    semester: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Bookmark(Base):
    __tablename__ = "bookmarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    material_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pdf_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    book_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PersonalNote(Base):
    __tablename__ = "personal_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    material_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pdf_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    note_text: Mapped[Text] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReadingProgress(Base):
    __tablename__ = "reading_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    material_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pdf_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    book_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    current_page: Mapped[Optional[int]] = mapped_column(Integer, default=1)
    total_pages: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    progress_percent: Mapped[Optional[float]] = mapped_column(Float, default=0)
    last_read_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pdf_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[Text] = mapped_column(Text)
    sources: Mapped[Optional[Text]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExamPaperNew(Base):
    __tablename__ = "exam_papers_new"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    semester: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    year: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    file_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class ImportantQuestion(Base):
    __tablename__ = "important_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    chapter_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    question: Mapped[Text] = mapped_column(Text)
    answer: Mapped[Optional[Text]] = mapped_column(Text, nullable=True)
    explanation: Mapped[Optional[Text]] = mapped_column(Text, nullable=True)
    marks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    question_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)


class MCQ(Base):
    __tablename__ = "mcqs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    chapter_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    question: Mapped[Text] = mapped_column(Text)
    option_a: Mapped[str] = mapped_column(String(255))
    option_b: Mapped[str] = mapped_column(String(255))
    option_c: Mapped[str] = mapped_column(String(255))
    option_d: Mapped[str] = mapped_column(String(255))
    correct_option: Mapped[str] = mapped_column(String(1))
    explanation: Mapped[Optional[Text]] = mapped_column(Text, nullable=True)


class MockTest(Base):
    __tablename__ = "mock_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    semester: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, default=60)
    total_marks: Mapped[Optional[int]] = mapped_column(Integer, default=100)


class MockTestQuestion(Base):
    __tablename__ = "mock_test_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mock_test_id: Mapped[int] = mapped_column(Integer, index=True)
    mcq_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    question: Mapped[Text] = mapped_column(Text)
    option_a: Mapped[str] = mapped_column(String(255))
    option_b: Mapped[str] = mapped_column(String(255))
    option_c: Mapped[str] = mapped_column(String(255))
    option_d: Mapped[str] = mapped_column(String(255))
    correct_option: Mapped[str] = mapped_column(String(1))
    marks: Mapped[Optional[int]] = mapped_column(Integer, default=1)


class TestAttempt(Base):
    __tablename__ = "test_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    mock_test_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    attempt_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, default=0)
    total: Mapped[Optional[float]] = mapped_column(Float, default=0)
    correct: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    incorrect: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class TestResult(Base):
    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), index=True)
    test_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, default=0)
    total: Mapped[Optional[float]] = mapped_column(Float, default=0)
    percentage: Mapped[Optional[float]] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StudentProgress(Base):
    __tablename__ = "student_progress"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    total_tests: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    average_score: Mapped[Optional[float]] = mapped_column(Float, default=0)
    pdf_read_count: Mapped[Optional[int]] = mapped_column(Integer, default=0)
    last_active: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class DownloadEvent(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    content_type: Mapped[str] = mapped_column(String(50), index=True)
    content_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TranslationCache(Base):
    __tablename__ = "translation_cache"
    __table_args__ = (UniqueConstraint("source_hash", "target_lang", name="uq_translation_source_lang"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    target_lang: Mapped[str] = mapped_column(String(10), index=True)
    source_text: Mapped[str] = mapped_column(Text)
    translated_text: Mapped[str] = mapped_column(Text)
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


def ensure_payment_columns():
    inspector = inspect(engine)
    if "wallet_transactions" not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns("wallet_transactions")}
    with engine.begin() as connection:
        if "reference" not in existing:
            connection.execute(
                text('ALTER TABLE wallet_transactions ADD COLUMN "reference" VARCHAR(120)')
            )
        connection.execute(
            text(
                'CREATE UNIQUE INDEX IF NOT EXISTS ix_wallet_transactions_reference '
                'ON wallet_transactions("reference")'
            )
        )


try:
    ensure_payment_columns()
except Exception as exc:
    print(f"Warning: could not migrate wallet_transactions table automatically: {exc}")


def ensure_subcategory_columns():
    inspector = inspect(engine)
    if "subcategories" not in inspector.get_table_names():
        return

    existing = {column["name"] for column in inspector.get_columns("subcategories")}
    with engine.begin() as connection:
        if "semester" not in existing:
            connection.execute(
                text('ALTER TABLE subcategories ADD COLUMN "semester" INTEGER')
            )
            connection.execute(
                text(
                    'CREATE INDEX IF NOT EXISTS ix_subcategories_semester '
                    'ON subcategories("semester")'
                )
            )


try:
    ensure_subcategory_columns()
except Exception as exc:
    print(f"Warning: could not migrate subcategories table automatically: {exc}")


BSCIT_SUBJECTS = {
    1: [
        "Imperative Programming",
        "Digital Electronics",
        "Operating Systems",
        "Discrete Mathematics",
        "Communication Skills",
    ],
    2: [
        "Object Oriented Programming",
        "Numerical and Statistical Methods",
        "Web Programming",
        "Fundamentals of Microprocessor and Microcontrollers",
        "Green Computing",
    ],
    3: [
        "Database Management Systems",
        "Data Structures",
        "Computer Networks",
        "Applied Mathematics",
        "Python Programming",
    ],
    4: [
        "Core Java",
        "Computer Oriented Statistical Techniques",
        "Computer Graphics and Animation",
        "Software Engineering",
        "Computer Organization and Architecture",
    ],
    5: [
        "Advanced Web Programming",
        "Linux System Administration",
        "Enterprise Java",
        "Software Project Management",
        "Next Generation Technologies",
        "Internet of Things",
        "Artificial Intelligence",
        "Network Security",
        "Mobile Computing",
    ],
    6: [
        "Software Quality Assurance",
        "Business Intelligence",
        "Principles of Geographic Information Systems",
        "Cyber Laws",
        "IT Service Management",
        "Security in Computing",
        "Project Work",
    ],
}


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
            for semester, subjects in BSCIT_SUBJECTS.items():
                class_level = (semester + 1) // 2
                for subject in subjects:
                    db.add(
                        SubCategory(
                            category_id=1,
                            class_level=class_level,
                            semester=semester,
                            name=subject,
                        )
                    )

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


class DownloadInput(BaseModel):
    user_id: Optional[str] = None
    content_type: str = "Unknown"
    content_id: Optional[int] = None
    title: Optional[str] = None


class SubCategoryInput(BaseModel):
    name: str
    category_id: int = 1
    class_level: Optional[int] = None
    semester: Optional[int] = None


class BookInput(BaseModel):
    subcat_id: int
    bookname: str
    referencename: Optional[str] = None
    image: Optional[str] = None


class NoteInput(BaseModel):
    subcat_id: int
    chapter_num: Optional[int] = None
    chapter_name: Optional[str] = None
    image: Optional[str] = None


class QuestionPaperInput(BaseModel):
    subcat_id: int
    title: str
    tag: Optional[str] = None
    image: Optional[str] = None


class QuestionBankInput(BaseModel):
    subcat_id: int
    title: str
    pdf: Optional[str] = None


class PracticalInput(BaseModel):
    subcat_id: int
    syllabus_title: Optional[str] = None
    syllabusPdf: Optional[str] = None
    solutionPdf: Optional[str] = None


class UnitInput(BaseModel):
    subject_id: int
    unit_num: int
    title: str


class SyllabusTopicInput(BaseModel):
    subcat_id: int
    unit: int = 1
    topic: str


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
        "role": getattr(user, "role", "student") or "student",
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


def _guess_content_type(extension: str) -> str:
    guessed, _ = mimetypes.guess_type("file" + extension)
    return guessed or "application/octet-stream"


def save_upload(upload: UploadFile, allowed_extensions: set) -> str:
    original = os.path.basename(upload.filename or "file")
    extension = os.path.splitext(original)[1].lower()
    if extension not in allowed_extensions:
        raise HTTPException(status_code=400, detail="This file type is not allowed")

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original)
    filename = f"{int(datetime.now().timestamp() * 1000)}_{secrets.token_hex(4)}_{safe_name}"

    if S3_ENABLED:
        try:
            upload.file.seek(0)
            get_s3_client().upload_fileobj(
                upload.file,
                S3_BUCKET,
                filename,
                ExtraArgs={"ContentType": _guess_content_type(extension)},
            )
            return filename
        except Exception as exc:
            print(f"S3 upload failed, falling back to local disk: {exc}")

    destination = os.path.join(UPLOAD_DIR, filename)
    upload.file.seek(0)
    with open(destination, "wb") as output:
        shutil.copyfileobj(upload.file, output)

    return filename


def build_media_url(key: str, expires: int = None) -> str:
    """Return a temporary signed URL for a stored object key."""
    if not key:
        return ""
    if re.match(r"^https?://", key, re.IGNORECASE):
        return key
    if S3_ENABLED:
        return get_s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": key.lstrip("/")},
            ExpiresIn=expires or S3_URL_TTL_SECONDS,
        )
    return f"/uploads/{key.lstrip('/')}"


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


@app.get("/users")
def get_users(db: Annotated[Session, Depends(get_db)] = None):
    rows = db.query(User).order_by(User.id.desc()).all()
    return [user_payload(row) for row in rows]


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
    class_level: Annotated[Optional[int], Query(alias="class")] = None,
    category_id: int = 1,
    semester: Optional[int] = None,
    db: Annotated[Session, Depends(get_db)] = None,
):
    query = db.query(SubCategory).filter(SubCategory.category_id == category_id)
    if class_level is not None:
        query = query.filter(SubCategory.class_level == class_level)
    if semester is not None:
        query = query.filter(SubCategory.semester == semester)
    rows = query.order_by(SubCategory.semester, SubCategory.id).all()
    return {
        "status": "success",
        "data": [
            {
                "id": row.id,
                "name": row.name,
                "class_level": row.class_level,
                "semester": row.semester,
            }
            for row in rows
        ],
    }


@app.post("/subcategories")
def create_subcategory(data: SubCategoryInput, db: Annotated[Session, Depends(get_db)]):
    class_level = data.class_level
    if class_level is None and data.semester:
        class_level = (data.semester + 1) // 2
    row = SubCategory(
        category_id=data.category_id,
        class_level=class_level,
        semester=data.semester,
        name=data.name,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id, "name": row.name, "class_level": row.class_level, "semester": row.semester}}


@app.put("/subcategories/{subcat_id}")
def update_subcategory(subcat_id: int, data: SubCategoryInput, db: Annotated[Session, Depends(get_db)]):
    row = db.get(SubCategory, subcat_id)
    if not row:
        raise HTTPException(status_code=404, detail="Subject not found")
    row.name = data.name
    row.category_id = data.category_id
    row.semester = data.semester
    row.class_level = data.class_level if data.class_level is not None else ((data.semester + 1) // 2 if data.semester else row.class_level)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id, "name": row.name, "class_level": row.class_level, "semester": row.semester}}


@app.delete("/subcategories/{subcat_id}")
def delete_subcategory(subcat_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(SubCategory, subcat_id)
    if not row:
        raise HTTPException(status_code=404, detail="Subject not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Subject deleted"}


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
def get_books(subcat_id: Optional[int] = None, db: Annotated[Session, Depends(get_db)] = None):
    query = db.query(Book)
    if subcat_id is not None:
        query = query.filter(Book.subcat_id == subcat_id)
    rows = query.order_by(Book.id).all()
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


@app.post("/books")
def create_book(data: BookInput, db: Annotated[Session, Depends(get_db)]):
    row = Book(subcat_id=data.subcat_id, bookname=data.bookname, referencename=data.referencename, image=data.image)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.put("/books/{book_id}")
def update_book(book_id: int, data: BookInput, db: Annotated[Session, Depends(get_db)]):
    row = db.get(Book, book_id)
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    row.subcat_id = data.subcat_id
    row.bookname = data.bookname
    row.referencename = data.referencename
    row.image = data.image
    db.commit()
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/books/{book_id}")
def delete_book(book_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(Book, book_id)
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Book deleted"}


@app.get("/notes")
def get_notes(subcat_id: Optional[int] = None, db: Annotated[Session, Depends(get_db)] = None):
    query = db.query(Note)
    if subcat_id is not None:
        query = query.filter(Note.subcat_id == subcat_id)
    rows = query.order_by(Note.id).all()
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


@app.post("/notes")
def create_note(data: NoteInput, db: Annotated[Session, Depends(get_db)]):
    row = Note(
        subcat_id=data.subcat_id,
        chapter_num=data.chapter_num,
        chapter_name=data.chapter_name,
        image=data.image,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.put("/notes/{note_id}")
def update_note(note_id: int, data: NoteInput, db: Annotated[Session, Depends(get_db)]):
    row = db.get(Note, note_id)
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    row.subcat_id = data.subcat_id
    row.chapter_num = data.chapter_num
    row.chapter_name = data.chapter_name
    row.image = data.image
    db.commit()
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/notes/{note_id}")
def delete_note(note_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(Note, note_id)
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Note deleted"}


@app.get("/practicals")
def get_practicals(subcat_id: Optional[int] = None, db: Annotated[Session, Depends(get_db)] = None):
    query = db.query(Practical)
    if subcat_id is not None:
        query = query.filter(Practical.subcat_id == subcat_id)
    rows = query.order_by(Practical.id).all()
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


@app.post("/practicals")
def create_practical(data: PracticalInput, db: Annotated[Session, Depends(get_db)]):
    row = Practical(
        subcat_id=data.subcat_id,
        syllabus_title=data.syllabus_title,
        syllabusPdf=data.syllabusPdf,
        solutionPdf=data.solutionPdf,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.put("/practicals/{practical_id}")
def update_practical(practical_id: int, data: PracticalInput, db: Annotated[Session, Depends(get_db)]):
    row = db.get(Practical, practical_id)
    if not row:
        raise HTTPException(status_code=404, detail="Practical not found")
    row.subcat_id = data.subcat_id
    row.syllabus_title = data.syllabus_title
    row.syllabusPdf = data.syllabusPdf
    row.solutionPdf = data.solutionPdf
    db.commit()
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/practicals/{practical_id}")
def delete_practical(practical_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(Practical, practical_id)
    if not row:
        raise HTTPException(status_code=404, detail="Practical not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Practical deleted"}


@app.get("/question-papers")
def get_question_papers(subcat_id: Optional[int] = None, db: Annotated[Session, Depends(get_db)] = None):
    query = db.query(QuestionPaper)
    if subcat_id is not None:
        query = query.filter(QuestionPaper.subcat_id == subcat_id)
    rows = query.order_by(QuestionPaper.id).all()
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


@app.post("/question-papers")
def create_question_paper(data: QuestionPaperInput, db: Annotated[Session, Depends(get_db)]):
    row = QuestionPaper(subcat_id=data.subcat_id, title=data.title, tag=data.tag, image=data.image)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.put("/question-papers/{paper_id}")
def update_question_paper(paper_id: int, data: QuestionPaperInput, db: Annotated[Session, Depends(get_db)]):
    row = db.get(QuestionPaper, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="Question paper not found")
    row.subcat_id = data.subcat_id
    row.title = data.title
    row.tag = data.tag
    row.image = data.image
    db.commit()
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/question-papers/{paper_id}")
def delete_question_paper(paper_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(QuestionPaper, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="Question paper not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Question paper deleted"}


@app.get("/question-bank")
def get_question_bank(subcat_id: Optional[int] = None, db: Annotated[Session, Depends(get_db)] = None):
    query = db.query(QuestionBankItem)
    if subcat_id is not None:
        query = query.filter(QuestionBankItem.subcat_id == subcat_id)
    rows = query.order_by(QuestionBankItem.id).all()
    return [
        {
            "id": row.id,
            "subcat_id": row.subcat_id,
            "title": row.title,
            "pdf": row.pdf,
        }
        for row in rows
    ]


@app.post("/question-bank")
def create_question_bank(data: QuestionBankInput, db: Annotated[Session, Depends(get_db)]):
    row = QuestionBankItem(subcat_id=data.subcat_id, title=data.title, pdf=data.pdf)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.put("/question-bank/{item_id}")
def update_question_bank(item_id: int, data: QuestionBankInput, db: Annotated[Session, Depends(get_db)]):
    row = db.get(QuestionBankItem, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Question bank item not found")
    row.subcat_id = data.subcat_id
    row.title = data.title
    row.pdf = data.pdf
    db.commit()
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/question-bank/{item_id}")
def delete_question_bank(item_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(QuestionBankItem, item_id)
    if not row:
        raise HTTPException(status_code=404, detail="Question bank item not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Question bank item deleted"}


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


@app.post("/syllabus/topics")
def create_syllabus_topic(data: SyllabusTopicInput, db: Annotated[Session, Depends(get_db)]):
    row = SyllabusTopic(subcat_id=data.subcat_id, unit=data.unit, topic=data.topic)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/syllabus/topics/{topic_id}")
def delete_syllabus_topic(topic_id: int, db: Annotated[Session, Depends(get_db)]):
    row = db.get(SyllabusTopic, topic_id)
    if not row:
        raise HTTPException(status_code=404, detail="Syllabus topic not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Syllabus topic deleted"}


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


NEWS_FEEDS = [
    {"source": "The Hindu", "url": "https://www.thehindu.com/education/feeder/default.rss"},
    {"source": "Hindustan Times", "url": "https://www.hindustantimes.com/feeds/rss/education/rssfeed.xml"},
    {"source": "Livemint", "url": "https://www.livemint.com/rss/education"},
    {"source": "Indian Express", "url": "https://indianexpress.com/section/education/feed/"},
]

NEWS_LIMIT = 30
NEWS_CACHE_TTL_SECONDS = 600
NEWS_IST = timezone(timedelta(hours=5, minutes=30))
NEWS_ATOM_NS = "{http://www.w3.org/2005/Atom}"
NEWS_MEDIA_NS = "{http://search.yahoo.com/mrss/}"
NEWS_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
NEWS_DC_NS = "{http://purl.org/dc/elements/1.1/}"

_news_cache: dict = {"fetched_at": 0.0, "items": []}


def _clean_news_text(value: Optional[str]) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_news_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = value.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(NEWS_IST)


def _first_news_text(item: ET.Element, tags: list) -> str:
    for tag in tags:
        node = item.find(tag)
        if node is not None:
            text = "".join(node.itertext()).strip()
            if text:
                return text
    return ""


def _extract_news_image(item: ET.Element, description_html: str) -> str:
    for tag in (f"{NEWS_MEDIA_NS}content", f"{NEWS_MEDIA_NS}thumbnail"):
        for node in item.findall(tag):
            url = node.attrib.get("url")
            if url:
                return url

    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.attrib.get("url", "")
        is_image = enclosure.attrib.get("type", "").startswith("image")
        if url and (is_image or re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url, re.I)):
            return url

    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description_html or "", re.I)
    return match.group(1) if match else ""


def _news_id(link: str, title: str) -> str:
    seed = link or title
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _parse_news_feed(source: str, payload: bytes) -> list:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []

    entries = root.findall(".//item")
    is_atom = False
    if not entries:
        entries = root.findall(f".//{NEWS_ATOM_NS}entry")
        is_atom = True

    items = []
    for entry in entries:
        if is_atom:
            title = _first_news_text(entry, [f"{NEWS_ATOM_NS}title", "title"])
            link = ""
            for link_node in entry.findall(f"{NEWS_ATOM_NS}link"):
                if link_node.attrib.get("rel", "alternate") == "alternate":
                    link = link_node.attrib.get("href", "")
                    break
            raw_description = _first_news_text(entry, [f"{NEWS_ATOM_NS}summary", f"{NEWS_ATOM_NS}content", "description"])
            raw_date = _first_news_text(entry, [f"{NEWS_ATOM_NS}published", f"{NEWS_ATOM_NS}updated", "pubDate"])
            author = _first_news_text(entry, [f"{NEWS_ATOM_NS}author", f"{NEWS_DC_NS}creator", "author"])
            category = _first_news_text(entry, [f"{NEWS_ATOM_NS}category", "category"])
        else:
            title = _first_news_text(entry, ["title"])
            link = _first_news_text(entry, ["link"])
            raw_description = _first_news_text(entry, ["description", f"{NEWS_CONTENT_NS}encoded"])
            raw_date = _first_news_text(entry, ["pubDate", f"{NEWS_DC_NS}date"])
            author = _first_news_text(entry, [f"{NEWS_DC_NS}creator", "author"])
            category = _first_news_text(entry, ["category"])

        title = _clean_news_text(title)
        if not title:
            continue

        description = _clean_news_text(raw_description)
        if len(description) > 600:
            description = description[:597].rstrip() + "..."

        parsed_date = _parse_news_date(raw_date)
        image = _extract_news_image(entry, raw_description)

        items.append({
            "id": _news_id(link, title),
            "title": title,
            "description": description,
            "image": image,
            "author": _clean_news_text(author) or source,
            "Date": parsed_date.strftime("%Y-%m-%d %H:%M:%S") if parsed_date else "",
            "link": link,
            "category": _clean_news_text(category) or "Education",
            "source": source,
            "_sort": parsed_date or datetime.min.replace(tzinfo=timezone.utc),
        })

    return items


async def _fetch_news_feed(client: httpx.AsyncClient, feed: dict) -> list:
    try:
        response = await client.get(feed["url"])
        response.raise_for_status()
        return _parse_news_feed(feed["source"], response.content)
    except Exception as exc:
        print(f"News feed error ({feed['source']}): {exc}")
        return []


async def _fetch_all_news() -> list:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=headers) as client:
        results = await asyncio.gather(*[_fetch_news_feed(client, feed) for feed in NEWS_FEEDS])

    merged = []
    seen_titles = set()
    seen_links = set()
    for feed_items in results:
        for item in feed_items:
            title_key = item["title"].lower()
            link_key = item["link"].split("?")[0].lower() if item["link"] else ""
            if title_key in seen_titles or (link_key and link_key in seen_links):
                continue
            seen_titles.add(title_key)
            if link_key:
                seen_links.add(link_key)
            merged.append(item)

    merged.sort(key=lambda x: x["_sort"], reverse=True)
    for item in merged:
        item.pop("_sort", None)
    return merged[:NEWS_LIMIT]


@app.get("/news")
async def get_news(refresh: bool = False):
    now = time.time()
    cache_fresh = (now - _news_cache["fetched_at"]) < NEWS_CACHE_TTL_SECONDS
    if cache_fresh and not refresh and _news_cache["items"]:
        return {"status": "success", "data": _news_cache["items"], "cached": True}

    items = await _fetch_all_news()
    if items:
        _news_cache["items"] = items
        _news_cache["fetched_at"] = now
        return {"status": "success", "data": items, "cached": False}

    if _news_cache["items"]:
        return {"status": "success", "data": _news_cache["items"], "cached": True, "stale": True}

    return {"status": "success", "data": [], "cached": False, "message": "News feeds are not reachable right now."}


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
        .filter(
            SubCategory.category_id == 1,
            or_(
                SubCategory.semester == semester,
                and_(
                    SubCategory.semester.is_(None),
                    SubCategory.class_level == class_level,
                ),
            ),
        )
        .order_by(SubCategory.id)
        .all()
    )
    return [{"id": row.id, "name": row.name, "semester": row.semester} for row in rows]


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


@app.get("/media/url")
def media_url(
    key: str = Query(..., description="Stored object key, e.g. 1712_ab12_notes.pdf"),
    user_id: Optional[str] = Query(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    db: Annotated[Session, Depends(get_db)] = None,
):
    """Return a short-lived signed URL for a private R2 object.

    A valid JWT (Authorization: Bearer <token>) ya valid user_id zaroori hai,
    taaki sirf logged-in/paid users hi PDFs access kar sakein.
    """
    if not key:
        raise HTTPException(status_code=400, detail="key is required")

    if re.match(r"^https?://", key, re.IGNORECASE):
        return {"url": key, "expires_in": None}

    authorized = False
    if credentials is not None:
        try:
            payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=["HS256"])
            authorized = db.get(User, int(payload["sub"])) is not None
        except (InvalidTokenError, ValueError, KeyError):
            authorized = False

    if not authorized and user_id:
        authorized = db.query(User).filter(User.user_id == user_id).first() is not None

    if not authorized:
        raise HTTPException(status_code=401, detail="Login required")

    return {"url": build_media_url(key), "expires_in": S3_URL_TTL_SECONDS}


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


async def _supabase_order_user(order_id: str) -> Optional[str]:
    """Fallback: agar webhook metadata me user_id nahi hai to orders table se le lo."""
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY and order_id):
        return None
    try:
        url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/orders"
        headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        }
        params = {"id": f"eq.{order_id}", "select": "user_id,metadata"}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            return None
        rows = response.json()
        if not rows:
            return None
        row = rows[0]
        if row.get("user_id"):
            return str(row["user_id"])
        metadata = row.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("user_id"):
            return str(metadata["user_id"])
    except Exception as exc:
        print("Supabase order lookup failed:", exc)
    return None


@app.post("/api/youpi-webhook")
async def youpi_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Gateway-Signature", "")
    if not YOUPI_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured")
    expected = "sha256=" + hmac.new(YOUPI_WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = str(payload.get("event") or "").upper()
    method = str(payload.get("method") or "UPI").upper()
    order_id = payload.get("orderId")
    payment_id = payload.get("paymentId")
    utr = payload.get("utr")
    metadata = payload.get("metadata") or {}

    if event != "SUCCESS":
        return {"received": True}

    # COD / counter events me paisa receive nahi hua, wallet credit nahi karna.
    if method and method != "UPI":
        return {"received": True, "credited": False, "reason": "NON_UPI_METHOD"}

    try:
        amount_paise = int(float(payload.get("amountPaise") or 0))
    except (TypeError, ValueError):
        amount_paise = 0
    amount = amount_paise / 100 if amount_paise else float(payload.get("amount") or 0)
    reference = str(payment_id or order_id or "").strip()

    if amount <= 0 or not reference:
        return {"received": True, "credited": False, "reason": "INVALID_PAYLOAD"}

    user_ref = None
    if isinstance(metadata, dict):
        user_ref = metadata.get("user_id")
    if not user_ref and order_id:
        user_ref = await _supabase_order_user(str(order_id))

    credited = False
    if user_ref:
        db = SessionLocal()
        try:
            user = find_user(db, str(user_ref))
            if user:
                existing = (
                    db.query(WalletTransaction)
                    .filter(WalletTransaction.reference == reference)
                    .first()
                )
                if existing:
                    return {"received": True, "credited": False, "reason": "ALREADY_PROCESSED"}

                user.wallet_balance = float(user.wallet_balance or 0) + amount
                db.add(WalletTransaction(
                    user_id=user.user_id or str(user.id),
                    transaction_type="Credit",
                    amount=amount,
                    purchase=f"Wallet recharge {utr or order_id or ''}".strip(),
                    status="Completed",
                    reference=reference,
                ))
                db.add(RechargeTransaction(
                    user_id=user.user_id or str(user.id),
                    payer_name=user.name,
                    amount=amount,
                    transaction_id=str(order_id) if order_id else None,
                    usertrant_id=str(utr) if utr else None,
                    status="Success",
                ))
                db.commit()
                credited = True
        except Exception as exc:
            db.rollback()
            print("Wallet credit failed:", exc)
            raise HTTPException(status_code=500, detail="Wallet credit failed")
        finally:
            db.close()

    # Supabase orders table bhi sync rakho (agar configured hai)
    if SUPABASE_URL and SUPABASE_SERVICE_KEY and order_id:
        try:
            url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/orders"
            headers = {
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
            params = {"id": f"eq.{order_id}"}
            data = {"status": "SUCCESS"}
            if utr:
                data["utr"] = str(utr)
            async with httpx.AsyncClient(timeout=10) as client:
                await client.patch(url, headers=headers, params=params, json=data)
        except Exception as e:
            print("Supabase update failed:", e)

    return {"received": True, "credited": credited}


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
# --- Smart College Learning App: New Endpoints ---

class BookmarkInput(BaseModel):
    user_id: str
    material_id: int = None
    pdf_id: int = None
    book_id: int = None
    title: str = None


class PersonalNoteInput(BaseModel):
    user_id: str
    material_id: int = None
    pdf_id: int = None
    page_no: int = None
    note_text: str


class ReadingProgressInput(BaseModel):
    user_id: str
    material_id: int = None
    pdf_id: int = None
    book_id: int = None
    current_page: int = 1
    total_pages: int = None
    progress_percent: float = 0


class AIChatInput(BaseModel):
    user_id: str = None
    conversation_id: int = None
    question: str
    subject_id: int = None
    pdf_id: int = None


@app.get("/chapters")
def get_chapters(subject_id: int, db: Session = Depends(get_db)):
    rows = db.query(Chapter).filter(Chapter.subject_id == subject_id).order_by(Chapter.chapter_num, Chapter.id).all()
    return [{"id": r.id, "subject_id": r.subject_id, "unit": r.unit, "chapter_name": r.chapter_name, "chapter_num": r.chapter_num} for r in rows]


@app.get("/units")
def get_units(subject_id: int, db: Session = Depends(get_db)):
    rows = db.query(Unit).filter(Unit.subject_id == subject_id).order_by(Unit.unit_num).all()
    return [{"id": r.id, "subject_id": r.subject_id, "unit_num": r.unit_num, "title": r.title} for r in rows]


@app.post("/units")
def create_unit(data: UnitInput, db: Session = Depends(get_db)):
    row = Unit(subject_id=data.subject_id, unit_num=data.unit_num, title=data.title)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"status": "success", "data": {"id": row.id}}


@app.put("/units/{unit_id}")
def update_unit(unit_id: int, data: UnitInput, db: Session = Depends(get_db)):
    row = db.get(Unit, unit_id)
    if not row:
        raise HTTPException(status_code=404, detail="Unit not found")
    row.subject_id = data.subject_id
    row.unit_num = data.unit_num
    row.title = data.title
    db.commit()
    return {"status": "success", "data": {"id": row.id}}


@app.delete("/units/{unit_id}")
def delete_unit(unit_id: int, db: Session = Depends(get_db)):
    row = db.get(Unit, unit_id)
    if not row:
        raise HTTPException(status_code=404, detail="Unit not found")
    db.delete(row)
    db.commit()
    return {"status": "success", "message": "Unit deleted"}


@app.get("/study-materials")
def get_study_materials(subject_id: int = None, chapter_id: int = None, unit_id: int = None, semester: int = None, db: Session = Depends(get_db)):
    query = db.query(StudyMaterial)
    if subject_id:
        query = query.filter(StudyMaterial.subject_id == subject_id)
    if chapter_id:
        query = query.filter(StudyMaterial.chapter_id == chapter_id)
    if unit_id:
        query = query.filter(StudyMaterial.unit_id == unit_id)
    if semester:
        query = query.filter(StudyMaterial.semester == semester)
    rows = query.order_by(StudyMaterial.id).all()
    return [{"id": r.id, "subject_id": r.subject_id, "chapter_id": r.chapter_id, "unit_id": r.unit_id, "title": r.title, "material_type": r.material_type, "file_path": r.file_path, "semester": r.semester} for r in rows]


@app.post("/bookmarks")
def add_bookmark(data: BookmarkInput, db: Session = Depends(get_db)):
    bookmark = Bookmark(user_id=data.user_id, material_id=data.material_id, pdf_id=data.pdf_id, book_id=data.book_id, title=data.title)
    db.add(bookmark)
    db.commit()
    db.refresh(bookmark)
    return {"status": "success", "id": bookmark.id}


@app.get("/bookmarks")
def get_bookmarks(user_id: str, db: Session = Depends(get_db)):
    rows = db.query(Bookmark).filter(Bookmark.user_id == user_id).order_by(Bookmark.id.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "material_id": r.material_id, "pdf_id": r.pdf_id, "book_id": r.book_id, "title": r.title, "created_at": fmt_datetime(r.created_at)} for r in rows]


@app.post("/personal-notes")
def add_personal_note(data: PersonalNoteInput, db: Session = Depends(get_db)):
    note = PersonalNote(user_id=data.user_id, material_id=data.material_id, pdf_id=data.pdf_id, page_no=data.page_no, note_text=data.note_text)
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"status": "success", "id": note.id}


@app.get("/personal-notes")
def get_personal_notes(user_id: str, pdf_id: int = None, db: Session = Depends(get_db)):
    query = db.query(PersonalNote).filter(PersonalNote.user_id == user_id)
    if pdf_id:
        query = query.filter(PersonalNote.pdf_id == pdf_id)
    rows = query.order_by(PersonalNote.id.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "material_id": r.material_id, "pdf_id": r.pdf_id, "page_no": r.page_no, "note_text": r.note_text, "created_at": fmt_datetime(r.created_at), "updated_at": fmt_datetime(r.updated_at)} for r in rows]


@app.post("/reading-progress")
def update_reading_progress(data: ReadingProgressInput, db: Session = Depends(get_db)):
    existing = db.query(ReadingProgress).filter(ReadingProgress.user_id == data.user_id, ReadingProgress.pdf_id == data.pdf_id).first()
    if not existing and data.book_id:
        existing = db.query(ReadingProgress).filter(ReadingProgress.user_id == data.user_id, ReadingProgress.book_id == data.book_id).first()
    if existing:
        existing.current_page = data.current_page
        existing.total_pages = data.total_pages
        existing.progress_percent = data.progress_percent
        existing.last_read_at = datetime.utcnow()
    else:
        existing = ReadingProgress(user_id=data.user_id, material_id=data.material_id, pdf_id=data.pdf_id, book_id=data.book_id, current_page=data.current_page, total_pages=data.total_pages, progress_percent=data.progress_percent)
        db.add(existing)
    db.commit()
    return {"status": "success"}


@app.get("/reading-progress")
def get_reading_progress(user_id: str, db: Session = Depends(get_db)):
    rows = db.query(ReadingProgress).filter(ReadingProgress.user_id == user_id).order_by(ReadingProgress.last_read_at.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "material_id": r.material_id, "pdf_id": r.pdf_id, "book_id": r.book_id, "current_page": r.current_page, "total_pages": r.total_pages, "progress_percent": r.progress_percent, "last_read_at": fmt_datetime(r.last_read_at)} for r in rows]


@app.post("/ai/chat")
def ai_chat(data: AIChatInput, db: Session = Depends(get_db)):
    conversation = None
    if data.conversation_id:
        conversation = db.get(AIConversation, data.conversation_id)
    if not conversation:
        conversation = AIConversation(user_id=data.user_id, title=data.question[:60], subject_id=data.subject_id, pdf_id=data.pdf_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    db.add(AIMessage(conversation_id=conversation.id, role="user", content=data.question))
    db.commit()

    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")

    try:
        if groq_key:
            try:
                payload = {"model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"), "messages": [{"role": "user", "content": data.question}], "temperature": 0.7}
                headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
                r = httpx.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=60)
                if r.status_code == 200:
                    j = r.json()
                    answer = j.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if answer:
                        db.add(AIMessage(conversation_id=conversation.id, role="assistant", content=answer))
                        db.commit()
                        return {"status": "success", "answer": answer, "conversation_id": conversation.id}
                print(r.status_code, r.text[:200])
            except Exception as ex:
                print(ex)
        if gemini_key:
            try:
                payload = {"contents": [{"parts": [{"text": data.question}]}]}
                model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
                r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}", json=payload, timeout=60)
                if r.status_code == 200:
                    j = r.json()
                    answer = j.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    if answer:
                        db.add(AIMessage(conversation_id=conversation.id, role="assistant", content=answer))
                        db.commit()
                        return {"status": "success", "answer": answer, "conversation_id": conversation.id}
                print(r.status_code, r.text[:200])
            except Exception as ex:
                print(ex)
        if openai_key:
            try:
                payload = {"model": os.getenv("OPENAI_MODEL", "gpt-3.5-turbo"), "messages": [{"role": "user", "content": data.question}], "temperature": 0.7}
                headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
                r = httpx.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=60)
                if r.status_code == 200:
                    j = r.json()
                    answer = j.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if answer:
                        db.add(AIMessage(conversation_id=conversation.id, role="assistant", content=answer))
                        db.commit()
                        return {"status": "success", "answer": answer, "conversation_id": conversation.id}
            except Exception as ex:
                print(ex)
    except Exception as ex:
        print(ex)

    return {"status": "error", "configured": False, "message": "AI backend not configured. No fake responses are provided. Please integrate an AI provider in the backend.", "conversation_id": conversation.id}


@app.get("/ai/conversations")
def ai_conversations(user_id: str, db: Session = Depends(get_db)):
    rows = db.query(AIConversation).filter(AIConversation.user_id == user_id).order_by(AIConversation.updated_at.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "title": r.title, "subject_id": r.subject_id, "pdf_id": r.pdf_id, "created_at": fmt_datetime(r.created_at)} for r in rows]


@app.get("/ai/messages")
def ai_messages(conversation_id: int, db: Session = Depends(get_db)):
    rows = db.query(AIMessage).filter(AIMessage.conversation_id == conversation_id).order_by(AIMessage.id).all()
    return [{"id": r.id, "conversation_id": r.conversation_id, "role": r.role, "content": r.content, "sources": r.sources, "created_at": fmt_datetime(r.created_at)} for r in rows]


@app.get("/exam-papers")
def get_exam_papers_new(semester: int = None, subject_id: int = None, db: Session = Depends(get_db)):
    query = db.query(ExamPaperNew)
    if semester:
        query = query.filter(ExamPaperNew.semester == semester)
    if subject_id:
        query = query.filter(ExamPaperNew.subject_id == subject_id)
    rows = query.order_by(ExamPaperNew.id).all()
    return [{"id": r.id, "semester": r.semester, "subject_id": r.subject_id, "title": r.title, "year": r.year, "file_path": r.file_path} for r in rows]


@app.get("/important-questions")
def get_important_questions(subject_id: int = None, chapter_id: int = None, db: Session = Depends(get_db)):
    query = db.query(ImportantQuestion)
    if subject_id:
        query = query.filter(ImportantQuestion.subject_id == subject_id)
    if chapter_id:
        query = query.filter(ImportantQuestion.chapter_id == chapter_id)
    rows = query.order_by(ImportantQuestion.id).all()
    return [{"id": r.id, "subject_id": r.subject_id, "chapter_id": r.chapter_id, "question": r.question, "answer": r.answer, "explanation": r.explanation, "marks": r.marks, "question_type": r.question_type} for r in rows]


@app.get("/mcqs")
def get_mcqs(subject_id: int = None, chapter_id: int = None, db: Session = Depends(get_db)):
    query = db.query(MCQ)
    if subject_id:
        query = query.filter(MCQ.subject_id == subject_id)
    if chapter_id:
        query = query.filter(MCQ.chapter_id == chapter_id)
    rows = query.order_by(MCQ.id).all()
    return [{"id": r.id, "subject_id": r.subject_id, "chapter_id": r.chapter_id, "question": r.question, "option_a": r.option_a, "option_b": r.option_b, "option_c": r.option_c, "option_d": r.option_d, "correct_option": r.correct_option, "explanation": r.explanation} for r in rows]


@app.get("/mock-tests")
def get_mock_tests(semester: int = None, subject_id: int = None, db: Session = Depends(get_db)):
    query = db.query(MockTest)
    if semester:
        query = query.filter(MockTest.semester == semester)
    if subject_id:
        query = query.filter(MockTest.subject_id == subject_id)
    rows = query.order_by(MockTest.id).all()
    return [{"id": r.id, "title": r.title, "subject_id": r.subject_id, "semester": r.semester, "duration_minutes": r.duration_minutes, "total_marks": r.total_marks} for r in rows]


@app.post("/mock-tests/start")
def start_mock_test(data: dict, db: Session = Depends(get_db)):
    mock_test_id = data.get("mock_test_id")
    user_id = data.get("user_id")
    attempt = TestAttempt(user_id=user_id, mock_test_id=mock_test_id, attempt_type="mock")
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    questions = db.query(MockTestQuestion).filter(MockTestQuestion.mock_test_id == mock_test_id).all()
    return {"status": "success", "attempt_id": attempt.id, "questions": [{"id": q.id, "question": q.question, "option_a": q.option_a, "option_b": q.option_b, "option_c": q.option_c, "option_d": q.option_d, "marks": q.marks} for q in questions]}


@app.post("/mock-tests/submit")
def submit_mock_test(data: dict, db: Session = Depends(get_db)):
    user_id = data.get("user_id")
    mock_test_id = data.get("mock_test_id")
    score = float(data.get("score", 0))
    total = float(data.get("total", 0))
    correct = int(data.get("correct", 0))
    incorrect = int(data.get("incorrect", 0))
    attempt = TestAttempt(user_id=user_id, mock_test_id=mock_test_id, attempt_type="mock", score=score, total=total, correct=correct, incorrect=incorrect, completed_at=datetime.utcnow())
    db.add(attempt)
    db.add(TestResult(user_id=user_id, test_name=data.get("test_name"), subject=data.get("subject"), score=score, total=total, percentage=round((score/total*100) if total else 0, 2)))
    progress = db.query(StudentProgress).filter(StudentProgress.user_id == user_id).first()
    if not progress:
        progress = StudentProgress(user_id=user_id)
        db.add(progress)
    progress.total_tests = (progress.total_tests or 0) + 1
    if progress.average_score:
        progress.average_score = round((progress.average_score + (score/total*100 if total else 0)) / 2, 2)
    else:
        progress.average_score = round((score/total*100 if total else 0), 2)
    progress.last_active = datetime.utcnow()
    db.commit()
    return {"status": "success", "message": "Test submitted successfully"}


@app.get("/test-results")
def get_test_results(user_id: str, db: Session = Depends(get_db)):
    rows = db.query(TestResult).filter(TestResult.user_id == user_id).order_by(TestResult.id.desc()).all()
    return [{"id": r.id, "user_id": r.user_id, "test_name": r.test_name, "subject": r.subject, "score": r.score, "total": r.total, "percentage": r.percentage, "created_at": fmt_datetime(r.created_at)} for r in rows]


@app.get("/student-progress")
def get_student_progress(user_id: str, db: Session = Depends(get_db)):
    progress = db.query(StudentProgress).filter(StudentProgress.user_id == user_id).first()
    if not progress:
        return {"total_tests": 0, "average_score": 0, "pdf_read_count": 0, "bookmarked_questions": 0}
    return {"total_tests": progress.total_tests or 0, "average_score": progress.average_score or 0, "pdf_read_count": progress.pdf_read_count or 0, "bookmarked_questions": 0}


@app.get("/quiz-history")
def get_quiz_history(user_id: str, db: Session = Depends(get_db)):
    rows = db.query(TestResult).filter(TestResult.user_id == user_id).order_by(TestResult.id.desc()).limit(20).all()
    return [{"id": r.id, "test_name": r.test_name, "subject": r.subject, "score": r.score, "total": r.total, "percentage": r.percentage, "created_at": fmt_datetime(r.created_at)} for r in rows]


@app.get("/learning-activity")
def get_learning_activity(user_id: str, db: Session = Depends(get_db)):
    bookmarks = db.query(Bookmark).filter(Bookmark.user_id == user_id).count()
    notes = db.query(PersonalNote).filter(PersonalNote.user_id == user_id).count()
    reads = db.query(ReadingProgress).filter(ReadingProgress.user_id == user_id).count()
    tests = db.query(TestAttempt).filter(TestAttempt.user_id == user_id).count()
    return {"bookmarks": bookmarks, "personal_notes": notes, "reading_progress": reads, "tests_attempted": tests}


def _active_user_ids_since(db: Session, since: datetime):
    active = set()

    def add_users(rows):
        for row in rows:
            value = row[0]
            if value is not None and str(value).strip():
                active.add(str(value))

    add_users(db.query(DownloadEvent.user_id).filter(DownloadEvent.created_at >= since, DownloadEvent.user_id.isnot(None)).distinct().all())
    add_users(db.query(ReadingProgress.user_id).filter(ReadingProgress.last_read_at >= since).distinct().all())
    add_users(db.query(TestAttempt.user_id).filter(TestAttempt.started_at >= since).distinct().all())
    add_users(db.query(Bookmark.user_id).filter(Bookmark.created_at >= since).distinct().all())
    add_users(db.query(PersonalNote.user_id).filter(PersonalNote.updated_at >= since).distinct().all())
    add_users(db.query(AIConversation.user_id).filter(AIConversation.updated_at >= since).distinct().all())
    add_users(db.query(StudentProgress.user_id).filter(StudentProgress.last_active >= since).distinct().all())
    return active


@app.post("/downloads", status_code=201)
def track_download(data: DownloadInput, db: Session = Depends(get_db)):
    content_type = (data.content_type or "Unknown").strip()[:50] or "Unknown"
    title = (data.title or "").strip()[:255] or None
    user_id = (data.user_id or "").strip() or None

    event = DownloadEvent(
        user_id=user_id,
        content_type=content_type,
        content_id=data.content_id,
        title=title,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {"status": "success", "message": "Download tracked", "id": event.id}


@app.get("/admin/analytics")
def admin_analytics(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    total_users = db.query(User).count()
    total_downloads = db.query(DownloadEvent).count()
    downloads_today = db.query(DownloadEvent).filter(DownloadEvent.created_at >= today_start).count()
    downloads_week = db.query(DownloadEvent).filter(DownloadEvent.created_at >= week_start).count()
    downloads_month = db.query(DownloadEvent).filter(DownloadEvent.created_at >= month_start).count()

    type_rows = (
        db.query(DownloadEvent.content_type, func.count(DownloadEvent.id))
        .group_by(DownloadEvent.content_type)
        .order_by(func.count(DownloadEvent.id).desc())
        .all()
    )
    downloads_by_type = [
        {"type": row[0] or "Unknown", "count": int(row[1] or 0)}
        for row in type_rows
    ]

    recent_rows = (
        db.query(DownloadEvent)
        .order_by(DownloadEvent.created_at.desc(), DownloadEvent.id.desc())
        .limit(10)
        .all()
    )
    recent_downloads = [
        {
            "id": row.id,
            "user_id": row.user_id,
            "content_type": row.content_type,
            "title": row.title,
            "created_at": fmt_datetime(row.created_at),
        }
        for row in recent_rows
    ]

    return {
        "total_users": total_users,
        "total_downloads": total_downloads,
        "downloads_today": downloads_today,
        "downloads_week": downloads_week,
        "downloads_month": downloads_month,
        "active_users_today": len(_active_user_ids_since(db, today_start)),
        "active_users_7d": len(_active_user_ids_since(db, week_start)),
        "active_users_30d": len(_active_user_ids_since(db, month_start)),
        "downloads_by_type": downloads_by_type,
        "recent_downloads": recent_downloads,
    }


@app.get("/solutions")
def get_solutions(subject_id: int = None, type: str = None, db: Session = Depends(get_db)):
    results = []
    if type in [None, "important"]:
        q = db.query(ImportantQuestion)
        if subject_id:
            q = q.filter(ImportantQuestion.subject_id == subject_id)
        for iq in q.all():
            if iq.answer or iq.explanation:
                results.append({"type": "important_question", "question": iq.question, "answer": iq.answer, "explanation": iq.explanation})
    if type in [None, "mcq"]:
        m = db.query(MCQ)
        if subject_id:
            m = m.filter(MCQ.subject_id == subject_id)
        for mq in m.all():
            results.append({"type": "mcq", "question": mq.question, "correct_option": mq.correct_option, "explanation": mq.explanation})
    return results


# ---------------------------------------------------------------------------
# UI translation (Gemini backed, DB cached)
# ---------------------------------------------------------------------------

SUPPORTED_LANGUAGES = {
    "hi": "Hindi",
    "mr": "Marathi",
    "gu": "Gujarati",
    "ta": "Tamil",
    "te": "Telugu",
}


class TranslateInput(BaseModel):
    texts: List[str] = Field(default_factory=list)
    target_lang: str


def _translation_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gemini_translate_batch(texts: List[str], target_lang: str) -> Optional[List[str]]:
    """Translate a batch of strings using Gemini, returning a list in the same order."""
    if not texts:
        return []
    if target_lang == "en":
        return list(texts)
    language_name = SUPPORTED_LANGUAGES.get(target_lang)
    if not language_name:
        return None

    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not gemini_key:
        print("Translation skipped: GEMINI_API_KEY is not configured.")
        return None

    instruction = (
        f"You are a translation engine for a mobile study app. Translate every string in the JSON array "
        f"below into {language_name}. Rules: keep the same order and length; keep brand/product names, "
        f"emails, URLs, numbers and code unchanged; keep UI text short and natural; do not add explanations. "
        f"Return ONLY a valid JSON array of translated strings with no markdown fences."
    )
    model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    try:
        payload = {
            "contents": [{"parts": [{"text": instruction + "\n\n" + json.dumps(texts, ensure_ascii=False)}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        r = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}",
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            print("Gemini translate error:", r.status_code, r.text[:200])
            return None
        raw = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != len(texts):
            print("Gemini translate malformed response")
            return None
        return [str(item) for item in parsed]
    except Exception as exc:
        print("Gemini translate failed:", exc)
        return None


def _openai_compatible_translate_batch(
    url: str, api_key: str, model: str, texts: List[str], language_name: str, provider: str
) -> Optional[List[str]]:
    """Translate via an OpenAI-compatible chat API (Groq, OpenAI)."""
    system = (
        f"You are a translation engine for a mobile study app. Translate every string in the user's "
        f"JSON array into {language_name}. Keep the same order and length; keep brand/product names, "
        f"emails, URLs, numbers and code unchanged; keep UI text short and natural; do not add "
        f"explanations. Respond ONLY with a JSON object shaped like "
        f'{{\"translations\": [\"...\"]}} containing the translated strings in order.'
    )
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(texts, ensure_ascii=False)},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"{provider} translate error:", r.status_code)
            return None
        content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        parsed = json.loads(content)
        items = parsed.get("translations") if isinstance(parsed, dict) else parsed
        if not isinstance(items, list) or len(items) != len(texts):
            print(f"{provider} translate malformed response")
            return None
        return [str(item) for item in items]
    except Exception as exc:
        print(f"{provider} translate failed:", exc)
        return None


def _groq_translate_batch(texts: List[str], target_lang: str) -> Optional[List[str]]:
    language_name = SUPPORTED_LANGUAGES.get(target_lang)
    api_key = os.getenv("GROQ_API_KEY")
    if not language_name or not api_key:
        return None
    model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    return _openai_compatible_translate_batch(
        "https://api.groq.com/openai/v1/chat/completions", api_key, model, texts, language_name, "Groq"
    )


def _openai_translate_batch(texts: List[str], target_lang: str) -> Optional[List[str]]:
    language_name = SUPPORTED_LANGUAGES.get(target_lang)
    api_key = os.getenv("OPENAI_API_KEY")
    if not language_name or not api_key:
        return None
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    return _openai_compatible_translate_batch(
        "https://api.openai.com/v1/chat/completions", api_key, model, texts, language_name, "OpenAI"
    )


def _llm_translate_batch(texts: List[str], target_lang: str) -> Optional[List[str]]:
    """Try each configured provider in order until one returns a valid translation."""
    if not texts:
        return []
    if target_lang == "en":
        return list(texts)
    if target_lang not in SUPPORTED_LANGUAGES:
        return None

    providers = []
    if os.getenv("GROQ_API_KEY"):
        providers.append(_groq_translate_batch)
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        providers.append(_gemini_translate_batch)
    if os.getenv("OPENAI_API_KEY"):
        providers.append(_openai_translate_batch)

    if not providers:
        print("Translation skipped: no GROQ_API_KEY, GEMINI_API_KEY or OPENAI_API_KEY configured.")
        return None

    for translator in providers:
        result = translator(texts, target_lang)
        if result:
            return result
    return None


@app.post("/translate")
def translate_texts(data: TranslateInput, db: Annotated[Session, Depends(get_db)]):
    target_lang = (data.target_lang or "").strip().lower()
    texts = list(data.texts or [])

    if not texts:
        return {"translations": [], "target_lang": target_lang}
    if len(texts) > 100:
        raise HTTPException(status_code=400, detail="Too many strings. Max 100 per request.")
    if target_lang == "en":
        return {"translations": texts, "target_lang": target_lang}
    if target_lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail="Unsupported target language.")

    cleaned = [str(t or "").strip()[:500] for t in texts]
    hashes = [_translation_hash(t) for t in cleaned]

    cached = {
        row.source_hash: row.translated_text
        for row in db.query(TranslationCache)
        .filter(TranslationCache.target_lang == target_lang, TranslationCache.source_hash.in_(hashes))
        .all()
    }

    unique_missing = {}
    for text, source_hash in zip(cleaned, hashes):
        if text and source_hash not in cached and source_hash not in unique_missing:
            unique_missing[source_hash] = text

    if unique_missing:
        missing_hashes = list(unique_missing.keys())
        for start in range(0, len(missing_hashes), 40):
            chunk_hashes = missing_hashes[start:start + 40]
            chunk_texts = [unique_missing[h] for h in chunk_hashes]
            translated_chunk = _llm_translate_batch(chunk_texts, target_lang)
            if not translated_chunk:
                continue
            for source_hash, source_text, translated_text in zip(chunk_hashes, chunk_texts, translated_chunk):
                translated_text = str(translated_text or "").strip()
                if not translated_text:
                    continue
                db.add(TranslationCache(
                    source_hash=source_hash,
                    target_lang=target_lang,
                    source_text=source_text,
                    translated_text=translated_text,
                ))
                cached[source_hash] = translated_text
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            print("Translation cache commit failed:", exc)

    translations = [cached.get(source_hash, original) for original, source_hash in zip(cleaned, hashes)]
    return {"translations": translations, "target_lang": target_lang}
