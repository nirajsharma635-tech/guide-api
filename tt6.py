from main import engine, SessionLocal, User
from sqlalchemy import text
db = SessionLocal()
a = db.query(User).filter(User.email=="admin@guide.com").first()
print(a is None)
if a: print(a.role)
db.close()
