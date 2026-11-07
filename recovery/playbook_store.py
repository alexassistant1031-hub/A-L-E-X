"""
playbook_store.py

SQLite store for recovery playbooks and history.

Tables:
    playbooks          fingerprint, plan_json, success_count, fail_count,
                       created_at, last_used
    recovery_history   error_id, fingerprint, tool, attempt, tier, plan_json,
                       outcome, timestamp
    circuit_state      tool, failure_count, opened_at, cooldown_until

A playbook is stored ONLY after a verified successful retry, so the store
never encodes a fix that hasn't been proven to work.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .recovery_models import RecoveryPlan, Tier

_DEFAULT_DB = Path.home() / ".local" / "state" / "alex" / "recovery.db"


def _utc_now() -> float:
    return time.time()


class PlaybookStore:
    """Thread-safe SQLite store for playbooks, recovery history, and circuit state."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        path = Path(db_path or _DEFAULT_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS playbooks (
                    fingerprint   TEXT PRIMARY KEY,
                    plan_json     TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    fail_count    INTEGER NOT NULL DEFAULT 0,
                    created_at    REAL NOT NULL,
                    last_used     REAL
                );
                CREATE TABLE IF NOT EXISTS recovery_history (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_id    TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    tool        TEXT NOT NULL,
                    attempt     INTEGER NOT NULL,
                    tier        TEXT NOT NULL,
                    plan_json   TEXT,
                    outcome     TEXT NOT NULL,
                    timestamp   REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS circuit_state (
                    tool            TEXT PRIMARY KEY,
                    failure_count   INTEGER NOT NULL DEFAULT 0,
                    opened_at       REAL,
                    cooldown_until  REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_history_fingerprint
                    ON recovery_history(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_history_tool
                    ON recovery_history(tool);
                """
            )
            self._conn.commit()

    # ── playbooks ──────────────────────────────────────────────────────

    def lookup(self, fingerprint: str) -> RecoveryPlan | None:
        """Return a stored playbook for ``fingerprint``, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT plan_json FROM playbooks WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return _plan_from_row(data)
    def record_success(self, fingerprint: str, plan: RecoveryPlan) -> None:
        """Store or update a playbook after a verified successful retry."""
        plan_json = _plan_to_json(plan)
        now = _utc_now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT success_count, fail_count, created_at FROM playbooks WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO playbooks "
                    "(fingerprint, plan_json, success_count, fail_count, created_at, last_used) "
                    "VALUES (?, ?, 1, 0, ?, ?)",
                    (fingerprint, plan_json, now, now),
                )
            else:
                success_count, fail_count, created_at = existing
                self._conn.execute(
                    "UPDATE playbooks SET plan_json = ?, success_count = ?, last_used = ? "
                    "WHERE fingerprint = ?",
                    (plan_json, success_count + 1, now, fingerprint),
                )
            self._conn.commit()

    def record_fail(self, fingerprint: str) -> None:
        """Increment fail_count for a playbook whose plan did not work."""
        with self._lock:
            self._conn.execute(
                "UPDATE playbooks SET fail_count = fail_count + 1, last_used = ? "
                "WHERE fingerprint = ?",
                (_utc_now(), fingerprint),
            )
            self._conn.commit()

    # ── history ────────────────────────────────────────────────────────

    def append_history(
        self,
        *,
        error_id: str,
        fingerprint: str,
        tool: str,
        attempt: int,
        tier: Tier,
        plan: RecoveryPlan | None,
        outcome: str,
    ) -> None:
        plan_json = _plan_to_json(plan) if plan is not None else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO recovery_history "
                "(error_id, fingerprint, tool, attempt, tier, plan_json, outcome, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    error_id, fingerprint, tool, attempt,
                    tier.value, plan_json, outcome, _utc_now(),
                ),
            )
            self._conn.commit()

    # ── circuit state ──────────────────────────────────────────────────

    def read_circuit(self, tool: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT failure_count, opened_at, cooldown_until FROM circuit_state WHERE tool = ?",
                (tool,),
            ).fetchone()
        if row is None:
            return None
        return {"failure_count": row[0], "opened_at": row[1], "cooldown_until": row[2]}

    def write_circuit(self, tool: str, failure_count: int, opened_at: float | None,
                      cooldown_until: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO circuit_state (tool, failure_count, opened_at, cooldown_until) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tool) DO UPDATE SET "
                "failure_count = excluded.failure_count, "
                "opened_at = excluded.opened_at, "
                "cooldown_until = excluded.cooldown_until",
                (tool, failure_count, opened_at, cooldown_until),
            )
            self._conn.commit()

    def clear_circuit(self, tool: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM circuit_state WHERE tool = ?", (tool,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _plan_to_json(plan: RecoveryPlan) -> str:
    return json.dumps(
        {
            "error_id": plan.error_id,
            "diagnosis": plan.diagnosis,
            "category": plan.category.value,
            "action": plan.action,
            "args": plan.args,
            "risk_level": plan.risk_level,
            "requires_confirmation": plan.requires_confirmation,
            "retry_allowed": plan.retry_allowed,
            "reason": plan.reason,
            "tier": plan.tier.value,
            "source": plan.source,
        },
        sort_keys=True,
    )


def _plan_from_row(data: dict[str, Any]) -> RecoveryPlan:
    from .recovery_models import ErrorCategory, Tier

    return RecoveryPlan(
        error_id=data.get("error_id", ""),
        diagnosis=data.get("diagnosis", ""),
        category=ErrorCategory(data.get("category", "permanent")),
        action=data.get("action", "give_up"),
        args=data.get("args", {}),
        risk_level=data.get("risk_level", "low"),
        requires_confirmation=bool(data.get("requires_confirmation", False)),
        retry_allowed=bool(data.get("retry_allowed", True)),
        reason=data.get("reason", ""),
        tier=Tier(data.get("tier", "tier_4_give_up")),
        source=data.get("source", "playbook"),
    )
