from main import engine, SessionLocal, User, Base
from pwdlib import PasswordHash
from sqlalchemy import text
ph = PasswordHash.recommended()
with engine.begin() as conn:
    try:
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'student'"))
    except Exception as e:
        print(e)
db = SessionLocal()
a = db.query(User).filter(User.email=="admin@guide.com").first()
if not a:
    u = User(name="Admin", email="admin@guide.com", hashed_password=ph.hash("admin123"), role="admin")
    db.add(u)
    db.commit()
    print("created")
else:
    a.hashed_password = ph.hash("admin123")
    a.role = "admin"
    db.commit()
    print("updated", a.role)
db.close()
