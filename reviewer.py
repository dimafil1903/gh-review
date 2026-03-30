#!/usr/bin/env python3
"""
GitHub code reviewer — fetches diff, runs claude -p for review, posts to GitHub + Telegram
"""
import json
import os
import subprocess
import sys

import httpx

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CLAUDE_BIN = os.path.expanduser("~/.nvm/versions/node/v24.11.0/bin/claude")

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}


def gh_get_diff(path):
    r = httpx.get(
        f"https://api.github.com{path}",
        headers={**GH_HEADERS, "Accept": "application/vnd.github.v3.diff"},
        follow_redirects=True,
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def gh_post(path, data):
    r = httpx.post(
        f"https://api.github.com{path}",
        headers=GH_HEADERS,
        json=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    chunks = [text[i:i+4000] for i in range(0, min(len(text), 12000), 4000)]
    for chunk in chunks:
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except Exception as e:
            print(f"Telegram error: {e}", file=sys.stderr)


def call_claude(prompt):
    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {result.stderr[:500]}")
    return result.stdout.strip()


def review_pr(repo, pr_number, pr_title, pr_url, pr_author, base_branch, head_branch):
    print(f"Reviewing PR #{pr_number} in {repo}...")

    diff = gh_get_diff(f"/repos/{repo}/pulls/{pr_number}")
    if len(diff) > 50000:
        diff = diff[:50000] + "\n... [diff truncated]"

    prompt = f"""Ти — senior code reviewer. Зроби детальний code review для цього Pull Request.

Репо: {repo}
PR #{pr_number}: {pr_title}
Автор: {pr_author}
Гілки: {head_branch} → {base_branch}
URL: {pr_url}

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
    print(f"Review generated ({len(review)} chars)")

    try:
        gh_post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": review})
        print("Posted comment to PR ✅")
    except Exception as e:
        print(f"Failed to post PR comment: {e}", file=sys.stderr)

    send_telegram(f"🔍 *Code Review: [{repo}#{pr_number}]({pr_url})*\n_{pr_title}_\n\n{review}")
    print("Sent Telegram ✅")


def review_push(repo, branch, commits, pusher, compare_url):
    if not commits:
        return
    print(f"Reviewing push to {repo}/{branch}...")

    latest_sha = commits[-1]["id"]
    diff = gh_get_diff(f"/repos/{repo}/commits/{latest_sha}")
    if len(diff) > 50000:
        diff = diff[:50000] + "\n... [diff truncated]"

    commit_msgs = "\n".join([
        f"- {c['message'].splitlines()[0]} ({c['id'][:7]})"
        for c in commits[:10]
    ])

    prompt = f"""Ти — senior code reviewer. Зроби детальний code review для цього push.

Репо: {repo}
Гілка: {branch}
Автор: {pusher}
Коміти:
{commit_msgs}

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
    print(f"Review generated ({len(review)} chars)")

    try:
        gh_post(f"/repos/{repo}/commits/{latest_sha}/comments", {"body": review})
        print("Posted commit comment ✅")
    except Exception as e:
        print(f"Failed to post commit comment: {e}", file=sys.stderr)

    send_telegram(f"🔍 *Push Review: [{repo}]({compare_url})* → `{branch}`\n\n{review}")
    print("Sent Telegram ✅")


if __name__ == "__main__":
    event = json.loads(sys.argv[1])
    event_type = sys.argv[2]

    if event_type == "pull_request":
        pr = event["pull_request"]
        review_pr(
            repo=event["repository"]["full_name"],
            pr_number=pr["number"],
            pr_title=pr["title"],
            pr_url=pr["html_url"],
            pr_author=pr["user"]["login"],
            base_branch=pr["base"]["ref"],
            head_branch=pr["head"]["ref"],
        )
    elif event_type == "push":
        review_push(
            repo=event["repository"]["full_name"],
            branch=event["ref"].replace("refs/heads/", ""),
            commits=event.get("commits", []),
            pusher=event["pusher"]["name"],
            compare_url=event.get("compare", ""),
        )
