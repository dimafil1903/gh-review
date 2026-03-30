"""
GitHub Webhook Server for Code Review
Receives push/PR events from GitHub, triggers OpenClaw agent for review
"""
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
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


def trigger_review(event_type: str, payload: dict):
    """Trigger reviewer.py subprocess for code review"""
    repo = payload.get("repository", {}).get("full_name", "unknown")

    if event_type == "pull_request":
        action = payload.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            logger.info(f"Ignoring PR action: {action}")
            return
    elif event_type == "push":
        ref = payload.get("ref", "")
        branch = ref.replace("refs/heads/", "")
        commits = payload.get("commits", [])
        if not commits:
            return
        if branch not in ("main", "master", "develop", "dev"):
            logger.info(f"Ignoring push to branch: {branch}")
            return
    else:
        return

    script = os.path.join(os.path.dirname(__file__), "reviewer.py")
    env = {**os.environ}

    # Load .env explicitly so subprocess gets all vars
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()

    logger.info(f"Spawning reviewer for {event_type} on {repo}")
    try:
        subprocess.Popen(
            [sys.executable, script, json.dumps(payload), event_type],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        logger.error(f"Failed to spawn reviewer: {e}")


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
    
    background_tasks.add_task(lambda: trigger_review(event_type, payload))
    return JSONResponse({"status": "accepted", "event": event_type})


@app.get("/health")
async def health():
    return {"status": "ok"}
