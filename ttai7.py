from fastapi.testclient import TestClient
from main import app
client = TestClient(app)
r = client.post("/ai/chat", json={"user_id":"1","question":"test"})
print(r.status_code)
print(r.json())
