"""
circuit_breaker.py

Per-tool circuit breaker: when a tool fails repeatedly across calls, pause
auto-recovery for that tool for a cooldown period to protect API quota and
avoid hot loops. Thread-safe.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class CircuitState:
    tool: str
    failure_count: int = 0
    opened_at: float | None = None
    cooldown_until: float = 0.0


class CircuitBreaker:
    """Opens a per-tool circuit after ``threshold`` failures.

    While open, automatic recovery is paused for that tool until
    ``cooldown_until``. A successful call resets the breaker.
    """

    def __init__(self, threshold: int = 5, cooldown: float = 300.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._states: dict[str, CircuitState] = {}
        self._lock = threading.Lock()

    def record_failure(self, tool: str) -> CircuitState:
        with self._lock:
            state = self._states.get(tool)
            if state is None:
                state = CircuitState(tool=tool)
                self._states[tool] = state
            state.failure_count += 1
            now = time.time()
            if state.failure_count >= self.threshold and state.opened_at is None:
                state.opened_at = now
                state.cooldown_until = now + self.cooldown
            return state

    def record_success(self, tool: str) -> None:
        with self._lock:
            if tool in self._states:
                del self._states[tool]

    def is_open(self, tool: str) -> bool:
        with self._lock:
            state = self._states.get(tool)
            if state is None or state.opened_at is None:
                return False
            now = time.time()
            if now >= state.cooldown_until:
                # Cooldown expired: half-open, allow one trial.
                state.opened_at = None
                state.failure_count = 0
                return False
            return True

    def remaining_cooldown(self, tool: str) -> float:
        with self._lock:
            state = self._states.get(tool)
            if state is None or state.cooldown_until <= 0:
                return 0.0
            return max(0.0, state.cooldown_until - time.time())