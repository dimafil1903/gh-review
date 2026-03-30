# gh-review

GitHub webhook server that triggers an AI code review on every push/PR.

## How it works

1. GitHub sends a webhook event (push or pull_request)
2. The FastAPI server receives it and triggers an OpenClaw isolated agent
3. The agent fetches the diff, reviews the code, posts a comment to GitHub, and sends a summary to Telegram

## Setup

```bash
cp .env.example .env
# Fill in your values
uv run uvicorn webhook_server:app --host 127.0.0.1 --port 9876
```

## Environment variables

See `.env.example`
