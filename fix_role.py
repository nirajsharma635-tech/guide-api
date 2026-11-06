from main import engine
from sqlalchemy import text
with engine.begin() as conn:
    try:
        conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT 'student'"))
    except Exception as e:
        print(e)
with engine.begin() as conn:
    try:
        conn.execute(text("UPDATE users SET role='admin' WHERE email='admin@guide.com'"))
    except Exception as e:
        print(e)
