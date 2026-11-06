import sqlite3
from pwdlib import PasswordHash
ph = PasswordHash.recommended()
conn = sqlite3.connect("D:/Niraj/Guide/Guide-api/college.db")
c = conn.cursor()
c.execute("UPDATE users SET hashed_password=?, role='admin' WHERE email='admin@guide.com'", (ph.hash("admin123"),))
conn.commit()
c.execute("SELECT email, role, hashed_password FROM users WHERE email='admin@guide.com'")
r = c.fetchone()
print(r[0], r[1])
print(ph.verify("admin123", r[2]))
conn.close()
