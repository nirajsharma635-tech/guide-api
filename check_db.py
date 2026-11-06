import sqlite3
from pwdlib import PasswordHash
ph = PasswordHash.recommended()
conn = sqlite3.connect("D:/Niraj/Guide/Guide-api/api_test.db")
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
print(c.fetchall())
conn.close()
