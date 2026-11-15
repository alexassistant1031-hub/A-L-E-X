#!/usr/bin/env python3
"""
scripts/email_check.py

One-time connectivity check for Alex's email account.

Run from the project root after filling EMAIL_ADDRESS and EMAIL_APP_PASSWORD
in .env.local:

    .venv/bin/python scripts/email_check.py

It checks both directions without sending a message:

* IMAP login, folders, and the newest INBOX message (reading).
* SMTP login on the configured port (sending credentials).

That matters because Gmail can accept IMAP reads while rejecting SMTP, or the
other way around.  The script never prints the password.  It sends no mail
unless ``--send-test`` is passed, which sends one message to the account
itself to prove the full send + Sent Mail copy path.  Exit code 0 means every
check succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from tools.email.tool import (  # noqa: E402  (after .env is loaded)
    EmailConfig,
    EmailError,
    EmailTool,
    connect_smtp,
)

_CREDENTIAL_KEYS = ("EMAIL_ADDRESS", "EMAIL_APP_PASSWORD")


def credential_report(root: Path = ROOT) -> list[str]:
    """Describe where the email credentials came from, in operator terms.

    ``main.py`` loads ``.env`` first and ``.env.local`` second with override, so
    empty placeholders in ``.env`` are harmless for Alex itself.  They are not
    harmless for a one-off script that loads only ``.env``: it logs in with an
    empty password and Gmail answers ``535 ... Authentication failed``, which
    looks like a broken app password.  This report names the file that actually
    supplied each value and warns when a placeholder is left behind.
    """
    env_values = dotenv_values(root / ".env")
    local_values = dotenv_values(root / ".env.local")

    lines = ["== credential sources =="]
    for key in _CREDENTIAL_KEYS:
        seen: list[str] = []
        for name, values in ((".env", env_values), (".env.local", local_values)):
            if key in values:
                seen.append(f"{name}={'empty' if not (values[key] or '').strip() else 'set'}")
        origin = "the process environment"
        if (local_values.get(key) or "").strip():
            origin = ".env.local"
        elif (env_values.get(key) or "").strip():
            origin = ".env"
        lines.append(f"{key}: {origin}" + (f" ({', '.join(seen)})" if seen else ""))

    empty_in_env = [
        key
        for key in _CREDENTIAL_KEYS
        if key in env_values and not (env_values.get(key) or "").strip()
    ]
    if empty_in_env:
        lines.append(
            "WARNING: .env leaves "
            + ", ".join(empty_in_env)
            + " empty. Alex is fine because it loads .env.local last, but a "
            "test that loads only .env will fail with \"Authentication "
            "failed\". Comment those lines out in .env or load .env.local too."
        )
    return lines


def check_smtp() -> bool:
    """Authenticate to SMTP without sending anything."""
    try:
        config = EmailConfig.from_env()
    except EmailError as exc:
        print(f"FAIL: {exc}")
        return False

    try:
        smtp = connect_smtp(config)
    except EmailError as exc:
        print(f"FAIL: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"FAIL: unexpected {type(exc).__name__}: {exc}")
        return False

    try:
        smtp.quit()
    except Exception:  # noqa: BLE001 - the login already succeeded
        pass
    print(f"OK: SMTP login as {config.address} succeeded "
          f"({config.smtp_host}:{config.smtp_port}).")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="also send a test message to the account itself",
    )
    args = parser.parse_args()

    for line in credential_report():
        print(line)
    print()

    tool = EmailTool()
    ok = True

    print("== account / IMAP ==")
    result = await tool.execute(action="me")
    print(result)
    if result.startswith("Email: ") or result.startswith("Email request failed"):
        ok = False

    print("\n== folders ==")
    folders = await tool.execute(action="folders", limit=40)
    print(folders)
    if folders.startswith("Email: ") or folders.startswith("Email request failed"):
        ok = False

    print("\n== newest inbox message ==")
    inbox = await tool.execute(action="read", folder="INBOX", limit=1)
    print(inbox)
    if inbox.startswith("Email: ") or inbox.startswith("Email request failed"):
        ok = False

    print("\n== SMTP login ==")
    ok = check_smtp() and ok

    if args.send_test:
        print("\n== full send test (to the account itself) ==")
        config = EmailConfig.from_env()
        result = await tool.execute(
            action="send",
            to=config.address,
            subject=f"Alex email check {int(time.time())}",
            body="Automated send test from scripts/email_check.py. Safe to ignore.",
            confirmed=True,
        )
        print(result)
        if not result.startswith("Sent to"):
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
