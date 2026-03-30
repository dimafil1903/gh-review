#!/usr/bin/env python3
"""
GitHub code reviewer — fetches diff, runs claude -p - for review, posts to GitHub + Telegram
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

CLAUDE_BIN = (
    os.environ.get("CLAUDE_BIN_PATH", "").strip()
    or shutil.which("claude")
    or os.path.expanduser("~/.nvm/versions/node/v24.11.0/bin/claude")
)

MAX_DIFF_SIZE = 50_000
CLAUDE_TIMEOUT = 600
REVIEWED_BRANCHES = os.environ.get("REVIEW_BRANCHES", "main,master,develop,dev").split(",")

# Startup validation
_errors = []
if not GITHUB_TOKEN:
    _errors.append("GITHUB_TOKEN is not set")
if not os.path.exists(CLAUDE_BIN):
    _errors.append(f"Claude binary not found: {CLAUDE_BIN}")
if _errors:
    for e in _errors:
        logger.error(f"Config error: {e}")
    sys.exit(1)

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def gh_get_diff(path):
    try:
        r = httpx.get(
            f"https://api.github.com{path}",
            headers={**GH_HEADERS, "Accept": "application/vnd.github.v3.diff"},
            follow_redirects=True,
            timeout=30,
        )
        r.raise_for_status()
        return r.text
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"GitHub API error {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch diff: {e}")


def gh_post(path, data):
    try:
        r = httpx.post(
            f"https://api.github.com{path}",
            headers=GH_HEADERS,
            json=data,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"GitHub post error {e.response.status_code}: {e.response.text[:200]}")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping")
        return
    chunks = [text[i:i+4000] for i in range(0, min(len(text), 12000), 4000)]
    for chunk in chunks:
        for attempt in range(3):
            try:
                r = httpx.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": chunk,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                )
                r.raise_for_status()
                break
            except Exception as e:
                logger.warning(f"Telegram attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)


def call_claude(prompt, retries=3):
    """Pass prompt via stdin to avoid cmdline exposure in ps aux"""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "-"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT,
            )
            if result.returncode != 0:
                raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:300]}")
            output = result.stdout.strip()
            if not output:
                raise RuntimeError("claude returned empty output")
            return output
        except Exception as e:
            logger.warning(f"Claude attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise


def review_pr(repo, pr_number, pr_title, pr_url, pr_author, base_branch, head_branch):
    logger.info(f"Reviewing PR #{pr_number} in {repo}...")

    diff = gh_get_diff(f"/repos/{repo}/pulls/{pr_number}")
    truncated = ""
    if len(diff) > MAX_DIFF_SIZE:
        truncated = f"\n\n⚠️ Diff truncated: показано {MAX_DIFF_SIZE} з {len(diff)} символів"
        diff = diff[:MAX_DIFF_SIZE]

    prompt = f"""Ти — senior code reviewer. Зроби детальний code review для цього Pull Request.

Репо: {repo}
PR #{pr_number}: {pr_title}
Автор: {pr_author}
Гілки: {head_branch} → {base_branch}
URL: {pr_url}{truncated}

DIFF:
```diff
{diff}
```

Напиши review УКРАЇНСЬКОЮ мовою. Структура:

**🔴 Критичні проблеми** (баги, security, дата-лоси — якщо є)
**🟡 Попередження** (потенційні проблеми, edge cases)
**🟢 Хороші рішення** (що зроблено добре)
**💡 Пропозиції** (покращення, refactoring)
**📊 Підсумок** (загальна оцінка 1-10, рекомендація: approve/request changes)

Будь конкретним — вказуй файли та рядки. Фокус на реальних проблемах."""

    review = call_claude(prompt)
    logger.info(f"Review generated ({len(review)} chars)")

    try:
        gh_post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": review})
        logger.info("Posted comment to PR ✅")
    except Exception as e:
        logger.error(f"Failed to post PR comment: {e}")

    send_telegram(f"🔍 *Code Review: [{repo}#{pr_number}]({pr_url})*\n_{pr_title}_\n\n{review}")
    logger.info("Sent Telegram ✅")


def review_push(repo, branch, commits, pusher, compare_url):
    if not commits:
        logger.info("No commits, skipping")
        return

    logger.info(f"Reviewing push to {repo}/{branch} ({len(commits)} commits)...")

    latest_sha = commits[-1]["id"]

    if len(commits) > 1:
        # Multiple commits — use compare endpoint (before...after)
        # GitHub compare uses SHAs directly, no ~1 needed
        first_sha = commits[0]["id"]
        try:
            diff = gh_get_diff(f"/repos/{repo}/compare/{first_sha}...{latest_sha}")
        except Exception as e:
            logger.warning(f"Compare diff failed ({e}), falling back to latest commit")
            diff = gh_get_diff(f"/repos/{repo}/commits/{latest_sha}")
    else:
        diff = gh_get_diff(f"/repos/{repo}/commits/{latest_sha}")

    if not diff.strip():
        logger.info("Empty diff, skipping review")
        return

    truncated = ""
    if len(diff) > MAX_DIFF_SIZE:
        truncated = f"\n\n⚠️ Diff truncated: показано {MAX_DIFF_SIZE} з {len(diff)} символів"
        diff = diff[:MAX_DIFF_SIZE]

    commit_msgs = "\n".join([
        f"- {c['message'].splitlines()[0]} ({c['id'][:7]})"
        for c in commits[:10]
    ])

    prompt = f"""Ти — senior code reviewer. Зроби детальний code review для цього push.

Репо: {repo}
Гілка: {branch}
Автор: {pusher}
Коміти ({len(commits)}):
{commit_msgs}{truncated}

DIFF:
```diff
{diff}
```

Напиши review УКРАЇНСЬКОЮ мовою. Структура:

**🔴 Критичні проблеми** (баги, security, дата-лоси — якщо є)
**🟡 Попередження** (потенційні проблеми, edge cases)
**🟢 Хороші рішення** (що зроблено добре)
**💡 Пропозиції** (покращення, refactoring)
**📊 Підсумок** (загальна оцінка 1-10)

Будь конкретним — вказуй файли та рядки."""

    review = call_claude(prompt)
    logger.info(f"Review generated ({len(review)} chars)")

    try:
        gh_post(f"/repos/{repo}/commits/{latest_sha}/comments", {"body": review})
        logger.info("Posted commit comment ✅")
    except Exception as e:
        logger.error(f"Failed to post commit comment: {e}")

    send_telegram(f"🔍 *Push Review: [{repo}]({compare_url})* → `{branch}`\n\n{review}")
    logger.info("Sent Telegram ✅")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        logger.error("Usage: reviewer.py <json_payload> <event_type>")
        sys.exit(1)

    try:
        event = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON payload: {e}")
        sys.exit(1)

    event_type = sys.argv[2]

    if event_type == "pull_request":
        pr = event.get("pull_request", {})
        review_pr(
            repo=event["repository"]["full_name"],
            pr_number=pr["number"],
            pr_title=pr.get("title", ""),
            pr_url=pr.get("html_url", ""),
            pr_author=pr.get("user", {}).get("login", "unknown"),
            base_branch=pr.get("base", {}).get("ref", ""),
            head_branch=pr.get("head", {}).get("ref", ""),
        )
    elif event_type == "push":
        review_push(
            repo=event["repository"]["full_name"],
            branch=event["ref"].replace("refs/heads/", ""),
            commits=event.get("commits", []),
            pusher=event.get("pusher", {}).get("name", "unknown"),
            compare_url=event.get("compare", ""),
        )
    else:
        logger.error(f"Unknown event type: {event_type}")
        sys.exit(1)
