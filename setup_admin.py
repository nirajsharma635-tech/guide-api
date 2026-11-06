import sqlite3
from pwdlib import PasswordHash
ph = PasswordHash.recommended()
conn = sqlite3.connect("D:/Niraj/Guide/Guide-api/college.db")
c = conn.cursor()
try:
    c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'student'")
    conn.commit()
except:
    pass
c.execute("SELECT id FROM users WHERE email='admin@guide.com'")
r = c.fetchone()
if not r:
    c.execute("INSERT INTO users (name,email,hashed_password,role) VALUES (?,?,?,?)", ("Admin","admin@guide.com",ph.hash("admin123"),"admin"))
    conn.commit()
else:
    c.execute("UPDATE users SET role='admin', hashed_password=? WHERE email='admin@guide.com'", (ph.hash("admin123"),))
    conn.commit()
c.execute("SELECT email,role FROM users WHERE email='admin@guide.com'")
print(c.fetchone())
conn.close()
