#!/usr/bin/env python3
"""
scripts/email_check.py

One-time connectivity check for Alex's email account.

Run from the project root after filling EMAIL_ADDRESS and EMAIL_APP_PASSWORD
in .env.local:

    .venv/bin/python scripts/email_check.py

It prints the account, folders, and the newest INBOX message.  It does not
send anything.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from tools.email_tool import EmailTool  # noqa: E402  (after .env is loaded)


async def main() -> int:
    tool = EmailTool()

    print("== account ==")
    print(await tool.execute(action="me"))

    print("\n== folders ==")
    print(await tool.execute(action="folders", limit=40))

    print("\n== newest inbox message ==")
    result = await tool.execute(action="read", folder="INBOX", limit=1)
    print(result)

    if result.startswith("Email: "):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
