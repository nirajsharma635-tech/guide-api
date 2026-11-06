from fastapi.testclient import TestClient
from main import app, SessionLocal, User
from pwdlib import PasswordHash
ph = PasswordHash.recommended()
db = SessionLocal()
a = db.query(User).filter(User.email=="admin@guide.com").first()
print(ph.verify("admin123", a.hashed_password))
db.close()
client = TestClient(app)
r = client.post("/login", json={"email":"admin@guide.com","password":"admin123"})
print(r.status_code)
print(r.json())
