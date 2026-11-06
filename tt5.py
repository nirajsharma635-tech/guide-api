from fastapi.testclient import TestClient
from main import app, SessionLocal, User
db = SessionLocal()
a = db.query(User).filter(User.email=="admin@guide.com").first()
print(a is None)
if a: print(a.role)
db.close()
