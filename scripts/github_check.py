#!/usr/bin/env python3
"""
scripts/github_check.py

Verify the GitHub token and repository without changing repository data.

Run from the project root:

    .venv/bin/python scripts/github_check.py

It checks read access to the repo and performs one non-destructive write probe
(creating a dangling Git blob, which does not create a commit or branch).  It
never prints the token.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

API = "https://api.github.com"


def _client(token: str) -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alex-github-check",
        },
        timeout=30,
    )


def _probe(client: httpx.Client, label: str, method: str, path: str, **kwargs) -> bool:
    response = client.request(method, path, **kwargs)
    ok = response.status_code < 400
    detail = ""
    if not ok:
        try:
            detail = response.json().get("message", "")
        except Exception:
            detail = response.text[:80]
        # An empty repository legitimately returns 404 for its contents.
        if path.endswith("/contents/") and response.status_code == 404:
            ok = True
            detail = "empty repository"
    print(f"{'PASS' if ok else 'FAIL'}  {label:34} {response.status_code}  {detail[:60]}")
    return ok


def main() -> int:
    token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    owner = os.getenv("GITHUB_OWNER", "").strip()
    repo = os.getenv("GITHUB_REPO", "").strip()
    if not token or not owner or not repo:
        print("Missing GH_TOKEN, GITHUB_OWNER or GITHUB_REPO in .env.local.")
        return 2

    slug = f"{owner}/{repo}"
    print(f"Repository: {slug}\n")

    results: list[bool] = []
    with _client(token) as client:
        user = client.get("/user")
        if user.status_code == 200:
            data = user.json()
            print(f"Signed in as: {data.get('login')} (id {data.get('id')})\n")
            results.append(True)
        else:
            print(f"FAIL  token identity: {user.status_code}\n")
            results.append(False)

        results.append(_probe(client, "repo metadata", "GET", f"/repos/{slug}"))
        results.append(_probe(client, "contents read", "GET", f"/repos/{slug}/contents/"))
        results.append(
            _probe(
                client,
                "contents write (blob probe)",
                "POST",
                f"/repos/{slug}/git/blobs",
                json={"content": "permission probe", "encoding": "utf-8"},
            )
        )
        results.append(_probe(client, "issues read", "GET", f"/repos/{slug}/issues"))
        results.append(_probe(client, "actions variables read", "GET", f"/repos/{slug}/actions/variables"))
        results.append(_probe(client, "actions secrets read", "GET", f"/repos/{slug}/actions/secrets"))
        results.append(_probe(client, "webhooks read", "GET", f"/repos/{slug}/hooks"))
        results.append(_probe(client, "administration read", "GET", f"/repos/{slug}/actions/permissions"))

    print()
    if all(results):
        print("All checks passed. The token can read and write this repository.")
        return 0
    print(
        "Some checks failed. Grant the missing permissions on the fine-grained "
        "token, then run this script again."
    )
    print("Required: Contents (write), Issues (write), Pull requests (write),")
    print("Actions (write), Administration (write), Secrets (write),")
    print("Webhooks (write), Workflows (write), Metadata (read).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
