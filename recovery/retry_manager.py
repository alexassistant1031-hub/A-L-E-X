"""
retry_manager.py

Bounded automatic retry: max attempts, exponential backoff, detection of
repeated identical errors, and immediate stop on permanent errors.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RetryManager:
    """Tracks retry attempts for a single tool call.

    Attributes:
        max_attempts: Maximum automatic attempts (default 3).
        backoff_base: Base seconds for exponential backoff (default 1.0).
    """

    max_attempts: int = 3
    backoff_base: float = 1.0

    attempt: int = 0
    last_fingerprint: str | None = None
    repeated_count: int = 0
    history: list[dict[str, object]] = field(default_factory=list)

    def record(self, fingerprint: str, category, outcome: str) -> None:
        """Record one attempt and update repeat detection."""
        self.attempt += 1
        entry = {
            "attempt": self.attempt,
            "fingerprint": fingerprint,
            "category": category.value if hasattr(category, "value") else str(category),
            "outcome": outcome,
            "timestamp": time.time(),
        }
        self.history.append(entry)

        if fingerprint == self.last_fingerprint:
            self.repeated_count += 1
        else:
            self.repeated_count = 1
            self.last_fingerprint = fingerprint

    def should_stop(self, category, fingerprint: str) -> tuple[bool, str]:
        """Return ``(stop, reason)`` for the current state.

        Stops when the retry limit is reached, on a repeated identical error,
        or on a permanent error category.
        """
        from .recovery_models import ErrorCategory

        cat = category if isinstance(category, ErrorCategory) else ErrorCategory(
            str(category)
        )
        if cat in (ErrorCategory.AUTH, ErrorCategory.PERMISSION):
            return True, f"{cat.value} errors are never auto-bypassed"
        if cat == ErrorCategory.PERMANENT:
            return True, "permanent error: no automatic recovery"
        if self.attempt >= self.max_attempts:
            return True, f"retry limit reached ({self.max_attempts} attempts)"
        if self.repeated_count >= 2:
            return True, "repeated identical error: loop stopped"
        return False, ""

    def backoff_seconds(self) -> float:
        """Exponential backoff for the next attempt."""
        return self.backoff_base * (2 ** (self.attempt - 1)) if self.attempt >= 1 else 0.0