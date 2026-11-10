import os
import sqlite3
import sys

from pwdlib import PasswordHash

email = (os.getenv("ADMIN_EMAIL") or "admin@guide.com").strip().lower()
password = os.getenv("ADMIN_PASSWORD") or ""
if len(password) < 12 or password.lower() in {"admin123", "password", "admin"}:
    sys.exit("Set ADMIN_PASSWORD to a strong password (12+ chars) before running this script.")

ph = PasswordHash.recommended()
conn = sqlite3.connect(os.getenv("DB_PATH") or "D:/Niraj/Guide/Guide-api/college.db")
c = conn.cursor()
c.execute("UPDATE users SET hashed_password=?, role='admin' WHERE email=?", (ph.hash(password), email))
conn.commit()
c.execute("SELECT email, role, hashed_password FROM users WHERE email=?", (email,))
r = c.fetchone()
if not r:
    sys.exit(f"No user found for {email}")
print(r[0], r[1])
print(ph.verify(password, r[2]))
conn.close()
