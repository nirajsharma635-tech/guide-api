from fastapi.testclient import TestClient
from main import app
client = TestClient(app)
r = client.post("/ai/chat", json={"user_id":"1","question":"What is 2+2?"})
print(r.text[:220])
