import os
import sys

from main import engine, SessionLocal, User
from pwdlib import PasswordHash
from sqlalchemy import text

email = (os.getenv("ADMIN_EMAIL") or "admin@guide.com").strip().lower()
password = os.getenv("ADMIN_PASSWORD") or ""
if len(password) < 12 or password.lower() in {"admin123", "password", "admin"}:
    sys.exit("Set ADMIN_PASSWORD to a strong password (12+ chars) before running this script.")

ph = PasswordHash.recommended()
with engine.begin() as conn:
    try:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'student'"))
    except Exception as e:
        print(e)
db = SessionLocal()
a = db.query(User).filter(User.email == email).first()
if not a:
    u = User(name="Admin", email=email, hashed_password=ph.hash(password), role="admin")
    db.add(u)
    db.commit()
    print("created")
else:
    a.hashed_password = ph.hash(password)
    a.role = "admin"
    db.commit()
    print("updated", a.role)
db.close()