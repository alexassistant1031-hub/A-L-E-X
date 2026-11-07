#!/usr/bin/env python3
"""
scripts/telegram_login.py

One-time Telethon login for Alex's Telegram account.

Run this once from the project root:

    .venv/bin/python scripts/telegram_login.py

It asks for the phone number, the login code Telegram sends to the app, and
the two-step verification password if one is set.  It then prints a
``TELEGRAM_SESSION=...`` line.  Paste that line into ``.env.local``.

The session string is a full-account credential.  Treat it like a password:
never commit it, never paste it into a chat, and terminate the device in
Telegram settings if it leaks.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

MISSING = """Telegram is not configured yet.

Add these to .env.local first:

    TELEGRAM_API_ID=...
    TELEGRAM_API_HASH=...

Get them from https://my.telegram.org -> API development tools.
"""


async def main() -> int:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    if not api_id_raw or not api_hash:
        print(MISSING, file=sys.stderr)
        return 1

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print(
            "Telethon is not installed. Run:\n"
            "  .venv/bin/python -m pip install telethon",
            file=sys.stderr,
        )
        return 1

    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("TELEGRAM_API_ID must be an integer.", file=sys.stderr)
        return 1

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            await client.start()

        me = await client.get_me()
        if me is None:
            print("Telegram login did not return an account.", file=sys.stderr)
            return 1

        username = f" (@{me.username})" if getattr(me, "username", None) else ""
        print(f"\nLogged in as {me.first_name or 'account'}{username}, id {me.id}.")
        print("\nAdd exactly this line to .env.local:\n")
        print(f"TELEGRAM_SESSION={client.session.save()}")
        print(
            "\nDo not share or commit that session string. It is equivalent to "
            "your account password."
        )
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
