"""
tools/email_listener.py

Live new-email announcements over IMAP IDLE.

IDLE is a standard IMAP feature: the server pushes a notice the moment a new
message arrives, so there is no polling and no paid push service.  Gmail closes
an IDLE session after about 29 minutes; this listener re-issues IDLE well
before that, and reconnects automatically after network errors.

The listener is intentionally notify-only.  It never replies to an email and
never follows instructions found inside one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from email.utils import parseaddr
from typing import Any, Awaitable, Callable

from controller import get_audit

from .email_tool import (
    DEFAULT_FOLDER,
    EmailConfig,
    EmailError,
    EmailRecord,
    connect_imap,
    fetch_raw,
    parse_email,
    search_uids,
    select_folder,
)

logger = logging.getLogger("alex.tools.email_listener")

IDLE_SECONDS = 60.0
RECONNECT_SECONDS = 10.0
MAX_BATCH = 10
LIVE_ENV = "EMAIL_LIVE"


def live_enabled() -> bool:
    return os.getenv(LIVE_ENV, "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _sender_allowed(record: EmailRecord, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    _, address = parseaddr(record.sender)
    address = (address or record.sender).strip().lower()
    if not address:
        return False
    for item in allowed:
        item = item.strip().lower()
        if item.startswith("@"):
            if address.endswith(item):
                return True
        elif address == item:
            return True
    return False


def _is_self_sent(record: EmailRecord, address: str) -> bool:
    """True when the message came from Alex's own mailbox.

    Sending to the configured address delivers the message back into INBOX.
    Announcing it would start a feedback loop: announce -> model replies or
    forwards -> another email -> another announcement.
    """
    _, sender = parseaddr(record.sender)
    return bool(address) and (sender or "").strip().lower() == address.strip().lower()


def _supports_idle(conn: Any) -> bool:
    for capability in getattr(conn, "capabilities", ()) or ():
        if isinstance(capability, (bytes, bytearray)):
            if bytes(capability).upper() == b"IDLE":
                return True
        elif str(capability).strip().upper() == "IDLE":
            return True
    return False


def _max_uid(uids: list[str]) -> int:
    values = [int(uid) for uid in uids if str(uid).isdigit()]
    return max(values, default=0)


class EmailListener:
    """Watch INBOX with IDLE and announce new messages."""

    def __init__(
        self,
        config: EmailConfig,
        on_emails: Callable[[list[EmailRecord]], Awaitable[None]],
        *,
        idle_seconds: float = IDLE_SECONDS,
        reconnect_seconds: float = RECONNECT_SECONDS,
    ) -> None:
        self._config = config
        self._on_emails = on_emails
        self._idle_seconds = idle_seconds
        self._reconnect_seconds = reconnect_seconds
        self._stop = threading.Event()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn: Any | None = None
        self._conn_lock = threading.Lock()
        self._ready = threading.Event()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._ready.clear()
        self._task = asyncio.create_task(
            self._run(), name="alex-email-listener"
        )

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        """Wait until the first IDLE baseline has been established."""
        return await asyncio.to_thread(self._ready.wait, timeout)

    async def stop(self) -> None:
        self._stop.set()
        self._ready.clear()
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            # Interrupt a blocking IDLE read so shutdown is prompt.
            with contextlib.suppress(Exception):
                conn.shutdown()

        task, self._task = self._task, None
        if task is None or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._watch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("email listener disconnected: %s", exc)
            if not self._stop.is_set():
                try:
                    await asyncio.sleep(self._reconnect_seconds)
                except asyncio.CancelledError:
                    raise

    # ── worker thread ─────────────────────────────────────────────────────────

    def _watch(self) -> None:
        self._ready.clear()
        conn = connect_imap(self._config)
        with self._conn_lock:
            self._conn = conn
        try:
            select_folder(conn, DEFAULT_FOLDER)
            last_uid = _max_uid(search_uids(conn, ["ALL"]))
            self._ready.set()
            logger.info(
                "email listener watching %s (from uid %d)",
                self._config.address,
                last_uid,
            )

            while not self._stop.is_set():
                if _supports_idle(conn):
                    with conn.idle(duration=self._idle_seconds) as idler:
                        for typ, _data in idler:
                            # EXISTS means the mailbox changed; process now
                            # instead of waiting for the IDLE timeout.
                            if str(typ).upper() == "EXISTS":
                                break
                else:
                    # Free fallback for servers without IDLE.
                    self._stop.wait(self._idle_seconds)
                if self._stop.is_set():
                    break

                uids = [
                    uid
                    for uid in search_uids(
                        conn, ["UID", f"{last_uid + 1}:*"]
                    )
                    if uid.isdigit() and int(uid) > last_uid
                ]
                if not uids:
                    continue

                uids = sorted(uids, key=int)[:MAX_BATCH]
                records: list[EmailRecord] = []
                for uid in uids:
                    raw = fetch_raw(conn, uid)
                    if raw:
                        record = parse_email(raw, uid)
                        if (
                            _sender_allowed(
                                record, self._config.allowed_senders
                            )
                            and not _is_self_sent(
                                record, self._config.address
                            )
                        ):
                            records.append(record)

                last_uid = max(last_uid, max(int(uid) for uid in uids))
                if records:
                    self._dispatch(records)
        finally:
            self._ready.clear()
            with self._conn_lock:
                self._conn = None
            try:
                conn.logout()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.shutdown()

    # ── delivery ──────────────────────────────────────────────────────────────

    def _dispatch(self, records: list[EmailRecord]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            get_audit("email").write(
                "email_live", count=len(records), folder=DEFAULT_FOLDER
            )
        except Exception as exc:
            logger.warning("email live audit unavailable: %s", exc)

        async def deliver() -> None:
            try:
                await self._on_emails(records)
            except Exception:
                logger.exception("email announcement callback failed")

        try:
            asyncio.run_coroutine_threadsafe(deliver(), loop)
        except RuntimeError:
            logger.debug("email listener loop is closed")


def start_email_listener(
    on_emails: Callable[[list[EmailRecord]], Awaitable[None]],
) -> EmailListener | None:
    """Start the listener when credentials and EMAIL_LIVE are present."""
    if not live_enabled():
        logger.info("email live notifications disabled by %s", LIVE_ENV)
        return None
    try:
        config = EmailConfig.from_env()
    except EmailError as exc:
        logger.info("email live notifications inactive: %s", exc)
        return None
    listener = EmailListener(config, on_emails)
    listener.start()
    logger.info("email live notifications enabled for %s", config.address)
    return listener


__all__ = [
    "EmailListener",
    "IDLE_SECONDS",
    "LIVE_ENV",
    "live_enabled",
    "start_email_listener",
]
