from fastapi.testclient import TestClient
from main import app
client = TestClient(app)
r = client.post("/ai/chat", json={"user_id":"1","question":"Say hi in one word"})
print(r.text[:180])
