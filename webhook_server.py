"""
GitHub Webhook Server for Code Review
Receives push/PR events from GitHub, triggers OpenClaw agent for review
"""
import hashlib
import hmac
import json
import logging
import os
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="gh-review webhook")

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
OPENCLAW_GATEWAY_URL = os.environ.get("OPENCLAW_GATEWAY_URL", "http://localhost:18789")
OPENCLAW_GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def verify_signature(payload: bytes, signature: str) -> bool:
    if not GITHUB_WEBHOOK_SECRET:
        return True
    mac = hmac.new(GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256)
    expected = f"sha256={mac.hexdigest()}"
    return hmac.compare_digest(expected, signature)


async def trigger_review(event_type: str, payload: dict):
    """Trigger OpenClaw isolated agent to do code review"""
    repo = payload.get("repository", {}).get("full_name", "unknown")
    
    if event_type == "pull_request":
        action = payload.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            logger.info(f"Ignoring PR action: {action}")
            return
        
        pr = payload.get("pull_request", {})
        pr_number = pr.get("number")
        pr_title = pr.get("title", "")
        pr_url = pr.get("html_url", "")
        pr_author = pr.get("user", {}).get("login", "unknown")
        base_branch = pr.get("base", {}).get("ref", "")
        head_branch = pr.get("head", {}).get("ref", "")
        
        message = f"""You are a senior code reviewer. Review this GitHub Pull Request and provide detailed feedback.

TASK: Code review for PR #{pr_number} in {repo}
PR Title: {pr_title}
Author: {pr_author}
Branch: {head_branch} → {base_branch}
PR URL: {pr_url}

Steps:
1. Fetch the PR diff using exec:
   curl -s -H "Authorization: token {GITHUB_TOKEN}" -H "Accept: application/vnd.github.v3.diff" https://api.github.com/repos/{repo}/pulls/{pr_number}

2. Analyze the diff carefully. Look for:
   - Bugs, logic errors, edge cases
   - Security vulnerabilities (SQL injection, XSS, auth issues, etc.)
   - Performance problems
   - Code style/readability issues
   - Missing error handling
   - Test coverage gaps
   - Architecture/design concerns

3. Write a detailed review in Ukrainian with clear sections:
   **🔴 Критичні проблеми** (якщо є)
   **🟡 Попередження**
   **🟢 Хороші рішення**
   **💡 Пропозиції**
   **Підсумок**

4. Post the review as a PR comment using exec:
   curl -s -X POST \\
     -H "Authorization: token {GITHUB_TOKEN}" \\
     -H "Content-Type: application/json" \\
     https://api.github.com/repos/{repo}/issues/{pr_number}/comments \\
     -d '{{"body": "<YOUR_REVIEW_HERE>"}}'

5. Send a summary to Telegram using the sessions_send tool with sessionKey="main" and include the repo name, PR number, title, and key findings. Start with "🔍 Code Review: [{repo}#{pr_number}]({pr_url})".

Be thorough but concise. Focus on actionable feedback."""

    elif event_type == "push":
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        commits = payload.get("commits", [])
        pusher = payload.get("pusher", {}).get("name", "unknown")
        
        if not commits:
            return
        
        # Only review pushes to main/master/develop
        if branch not in ("main", "master", "develop", "dev"):
            logger.info(f"Ignoring push to branch: {branch}")
            return
        
        commit_msgs = "\n".join([f"- {c.get('message','').split(chr(10))[0]} ({c.get('id','')[:7]})" for c in commits[:10]])
        compare_url = payload.get("compare", "")
        
        message = f"""You are a senior code reviewer. Review this GitHub push and provide detailed feedback.

TASK: Code review for push to {repo}/{branch}
Pusher: {pusher}
Commits:
{commit_msgs}
Compare URL: {compare_url}

Steps:
1. Fetch the diff for the latest commit using exec (get the last commit SHA from the list above):
   curl -s -H "Authorization: token {GITHUB_TOKEN}" -H "Accept: application/vnd.github.v3.diff" https://api.github.com/repos/{repo}/commits/<LATEST_SHA>

2. Analyze the diff carefully. Look for:
   - Bugs, logic errors, edge cases
   - Security vulnerabilities
   - Performance problems
   - Code style/readability issues
   - Missing error handling
   - Architecture/design concerns

3. Write a detailed review in Ukrainian with clear sections:
   **🔴 Критичні проблеми** (якщо є)
   **🟡 Попередження**
   **🟢 Хороші рішення**
   **💡 Пропозиції**
   **Підсумок**

4. Post the review as a commit comment using exec:
   curl -s -X POST \\
     -H "Authorization: token {GITHUB_TOKEN}" \\
     -H "Content-Type: application/json" \\
     https://api.github.com/repos/{repo}/commits/<LATEST_SHA>/comments \\
     -d '{{"body": "<YOUR_REVIEW_HERE>"}}'

5. Send a summary to Telegram using the sessions_send tool with sessionKey="main". Start with "🔍 Push Review: [{repo}]({compare_url}) → {branch}".

Be thorough but concise. Focus on actionable feedback."""

    else:
        return

    # Trigger OpenClaw cron agentTurn in isolated session
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                headers={
                    "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "tool": "cron",
                    "args": {
                        "action": "add",
                        "job": {
                            "name": f"gh-review-{repo.replace('/', '-')}-{event_type}",
                            "schedule": {"kind": "at", "at": "now"},
                            "payload": {
                                "kind": "agentTurn",
                                "message": message,
                                "timeoutSeconds": 300,
                            },
                            "sessionTarget": "isolated",
                            "delivery": {"mode": "none"},
                        },
                    },
                },
            )
            logger.info(f"OpenClaw cron response: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to trigger OpenClaw: {e}")


@app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    
    # Verify signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if GITHUB_WEBHOOK_SECRET and not verify_signature(body, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")
    
    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    
    logger.info(f"Received event: {event_type} (delivery: {delivery_id})")
    
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    background_tasks.add_task(trigger_review, event_type, payload)
    return JSONResponse({"status": "accepted", "event": event_type})


@app.get("/health")
async def health():
    return {"status": "ok"}
