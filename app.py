from fastapi import FastAPI, Request, Header
import requests, os

app = FastAPI()

AGENT_URL = os.getenv("AGENT_URL", "https://example-agent.com/handle-pr")

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/webhook")
async def github_webhook(request: Request, x_github_event: str = Header(None)):
    payload = await request.json()
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    pr_details = {
        "action": action,
        "pr_number": pr.get("number"),
        "author": pr.get("user", {}).get("login"),
        "repo_name": repo.get("full_name"),
        "pr_title": pr.get("title"),
        "pr_url": pr.get("html_url"),
    }

    print("📬 Received PR Event:", pr_details)

    # Forward PR details to your AI agent
    try:
        res = requests.post(AGENT_URL, json=pr_details)
        print("✅ Sent to agent:", res.status_code, res.text)
    except Exception as e:
        print("❌ Error sending to agent:", e)

    return {"message": "Webhook received"}
