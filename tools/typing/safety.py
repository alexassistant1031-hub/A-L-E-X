"""
tools/typing/safety.py

Safety layer for keyboard control.

Typing is the most dangerous capability Alex has: it can enter text into
password fields, terminals, chat windows and system dialogs. This module
enforces limits and an audit trail *before* any keystroke is emitted.

Policy:
  - hard cap on text length
  - destructive shell commands are refused outright
  - risky actions (long text, coordinate clicks, system-level hotkeys)
    require an explicit `confirmed=True` from the model, which in turn
    requires Alex to have asked the user out loud first
  - simple rate limiting prevents runaway key loops
  - every attempted action is appended to an audit log
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import deque

from controller import (
    AuditError,
    ControllerConfig,
    ControllerError,
    SecureAuditLog,
    get_audit,
)

logger = logging.getLogger("alex.tools.safety")

MAX_TEXT_LENGTH = 2000
MAX_ACTIONS_PER_MINUTE = 30
CONFIRM_THRESHOLD = 300  # chars above which confirmation is required

# Commands that should never be typed into a shell, even if the user asks.
_DESTRUCTIVE_PATTERNS = [
    r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=",
    r"\bdd\s+of=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
    r"\bchmod\s+-R\s+777\s+/(\s|$)",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r">\s*/dev/(sd|nvme|hd)",
]

# Hotkeys that can affect the whole system or the window manager.
_RISKY_HOTKEYS = ("ctrl+alt", "alt+f4", "super", "win+", "ctrl+shift+esc", "ctrl+alt+f")

_ALLOW_ENV = "ALEX_TYPING_ALLOW_DANGEROUS"
_CONFIRM_ALL_ENV = "ALEX_TYPING_CONFIRM_ALL"


class TypingGuard:
    """Validates and records keyboard actions."""

    def __init__(
        self,
        audit: SecureAuditLog | None = None,
        *,
        config: ControllerConfig | None = None,
    ) -> None:
        # The audit sink is owned by the controller and lives outside the
        # project tree.  Tests may inject a log backed by a temp directory.
        self._audit = audit
        self._config = config
        self._recent: deque[float] = deque()
        self._destructive = [re.compile(p, re.IGNORECASE) for p in _DESTRUCTIVE_PATTERNS]

    # ── audit availability ────────────────────────────────────────────────────

    def _log(self) -> SecureAuditLog:
        return self._audit or get_audit("typing", self._config)

    def ensure_audit(self) -> None:
        """Fail closed if the secure typing audit log cannot be opened.

        Typing is the most dangerous capability, so the caller must call this
        *before* emitting any keystroke.  Constructing the log creates and
        validates the private controller directory.
        """
        try:
            self._log()
        except (ControllerError, OSError) as exc:
            raise AuditError(
                f"secure typing audit log is unavailable: {exc}"
            ) from exc

    # ── validation ────────────────────────────────────────────────────────────

    def validate(self, text: str, click: str = "") -> tuple[bool, str]:
        """Return (allowed, reason). `reason` is user-facing when blocked."""
        if not text or not text.strip():
            return False, "Error: nothing to type."

        if len(text) > MAX_TEXT_LENGTH:
            return False, (
                f"Error: text is {len(text)} characters; the limit is "
                f"{MAX_TEXT_LENGTH}. Split the request into smaller parts."
            )

        if os.getenv(_ALLOW_ENV) != "1":
            for pattern in self._destructive:
                if pattern.search(text):
                    logger.warning("Blocked destructive typing: %r", text[:120])
                    return False, (
                        "Error: this command looks destructive and was blocked by "
                        "the safety policy. It cannot be typed automatically."
                    )

        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= MAX_ACTIONS_PER_MINUTE:
            return False, "Error: too many typing actions in the last minute. Wait a moment."
        self._recent.append(now)

        return True, ""

    def requires_confirmation(self, text: str, click: str = "") -> bool:
        """True when Alex must get an explicit yes from the user first."""
        if os.getenv(_CONFIRM_ALL_ENV) == "1":
            return True
        if len(text) > CONFIRM_THRESHOLD:
            return True
        if click:
            return True
        lowered = text.strip().lower()
        return any(lowered.startswith(k) or k in lowered for k in _RISKY_HOTKEYS)

    def confirmation_message(self, text: str) -> str:
        preview = text if len(text) <= 120 else text[:117] + "..."
        return (
            "CONFIRMATION REQUIRED. This action is potentially risky "
            f"(preview: {preview!r}). Ask the user out loud for permission, "
            "then call type_text again with confirmed=true. Do not call it "
            "again if the user has not answered."
        )

    # ── audit ─────────────────────────────────────────────────────────────────

    def record(self, text: str, speed: str, click: str, result: str) -> None:
        """Append one typing event to the controller-owned audit log.

        Raises :class:`controller.AuditError` if the log is unavailable.  The
        caller decides whether that is fatal (it is, before typing) or merely
        alarming (it is, after typing already happened).
        """
        try:
            self._log().write(
                "typing_action",
                text_preview=text[:200],
                length=len(text),
                speed=speed,
                click=click,
                result=result[:200],
            )
        except AuditError:
            raise
        except (ControllerError, OSError) as exc:
            raise AuditError(
                f"secure typing audit log is unavailable: {exc}"
            ) from exc
