import os
import sqlite3
import sys

from pwdlib import PasswordHash

email = (os.getenv("ADMIN_EMAIL") or "admin@guide.com").strip().lower()
password = os.getenv("ADMIN_PASSWORD") or ""
if len(password) < 12 or password.lower() in {"admin123", "password", "admin"}:
    sys.exit("Set ADMIN_PASSWORD to a strong password (12+ chars) before running this script.")

ph = PasswordHash.recommended()
conn = sqlite3.connect(os.getenv("DB_PATH") or "D:/Niraj/Guide/Guide-api/api_test.db")
c = conn.cursor()
try:
    c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'student'")
    conn.commit()
except Exception:
    pass
c.execute("SELECT id FROM users WHERE email=?", (email,))
r = c.fetchone()
if not r:
    c.execute(
        "INSERT INTO users (name,email,hashed_password,role) VALUES (?,?,?,?)",
        ("Admin", email, ph.hash(password), "admin"),
    )
    conn.commit()
else:
    c.execute(
        "UPDATE users SET role='admin', hashed_password=? WHERE email=?",
        (ph.hash(password), email),
    )
    conn.commit()
c.execute("SELECT email,role FROM users WHERE email=?", (email,))
print(c.fetchone())
conn.close()
