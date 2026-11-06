import httpx,os
from dotenv import load_dotenv
load_dotenv("D:/Niraj/Guide/Guide-api/.env")
key=os.getenv("GROQ_API_KEY")
r=httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization":f"Bearer {key}"})
print(r.status_code)
j=r.json()
for m in j.get("data",[])[:10]:
    print(m.get("id"))
