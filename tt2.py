from fastapi.testclient import TestClient
from main import app
client = TestClient(app)
r = client.post("/login", json={"email":"admin@guide.com","password":"admin123"})
print(r.status_code)
print(r.text)
