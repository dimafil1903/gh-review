#!/usr/bin/env python3
"""Register GitHub webhooks for all accessible repos"""
import json
import os
import urllib.request
import urllib.error

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://orangepi5plus.tail6d6678.ts.net/webhook")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
OWNER = os.environ.get("GITHUB_OWNER", "dimafil1903")

def gh_request(path, method="GET", data=None):
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code

# Get all repos owned by dimafil1903
repos, _ = gh_request(f"/user/repos?per_page=100&affiliation=owner")

print(f"Found {len(repos)} repos")

results = {"registered": [], "already_exists": [], "failed": []}

for repo in repos:
    full_name = repo["full_name"]
    if not full_name.startswith(OWNER + "/"):
        continue
    
    repo_name = repo["name"]
    
    # Check existing hooks
    hooks, status = gh_request(f"/repos/{full_name}/hooks")
    if status != 200:
        print(f"  ✗ {full_name}: can't list hooks ({status})")
        results["failed"].append(full_name)
        continue
    
    # Check if our webhook already exists
    existing = any(
        h.get("config", {}).get("url") == WEBHOOK_URL
        for h in hooks
    )
    if existing:
        print(f"  ✓ {full_name}: already registered")
        results["already_exists"].append(full_name)
        continue
    
    # Register webhook
    payload = {
        "name": "web",
        "active": True,
        "events": ["push", "pull_request"],
        "config": {
            "url": WEBHOOK_URL,
            "content_type": "json",
            "secret": WEBHOOK_SECRET,
            "insecure_ssl": "0"
        }
    }
    
    result, status = gh_request(f"/repos/{full_name}/hooks", method="POST", data=payload)
    if status in (200, 201):
        print(f"  ✅ {full_name}: webhook registered (id={result.get('id')})")
        results["registered"].append(full_name)
    else:
        err = result.get("message", str(result))
        print(f"  ✗ {full_name}: failed ({status}) — {err}")
        results["failed"].append(full_name)

print(f"\n📊 Summary:")
print(f"  Registered:     {len(results['registered'])}")
print(f"  Already exists: {len(results['already_exists'])}")
print(f"  Failed:         {len(results['failed'])}")
if results["failed"]:
    print(f"  Failed repos: {results['failed']}")
