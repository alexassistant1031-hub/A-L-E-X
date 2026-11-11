"""Tests for the SQLite playbook store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recovery.playbook_store import PlaybookStore
from recovery.recovery_models import (
    ErrorCategory, RecoveryPlan, SafetyTag, Tier,
)


def make_plan() -> RecoveryPlan:
    return RecoveryPlan(
        error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
        action="retry", args={}, risk_level="low",
        requires_confirmation=False, retry_allowed=True, reason="r",
        tier=Tier.RULES, source="rules",
    )


class PlaybookStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-recovery-test-")
        self.addCleanup(self._tmp.cleanup)
        self.store = PlaybookStore(Path(self._tmp.name) / "recovery.db")

    def test_lookup_missing_returns_none(self) -> None:
        self.assertIsNone(self.store.lookup("unknown-fingerprint"))

    def test_store_after_verified_retry(self) -> None:
        plan = make_plan()
        self.store.record_success("fp1", plan)
        got = self.store.lookup("fp1")
        self.assertIsNotNone(got)
        self.assertEqual(got.action, "retry")
        self.assertEqual(got.source, "rules")

    def test_record_fail_increments(self) -> None:
        plan = make_plan()
        self.store.record_success("fp1", plan)
        self.store.record_fail("fp1")
        got = self.store.lookup("fp1")
        self.assertIsNotNone(got)
        self.assertEqual(got.action, "retry")

    def test_history_append_and_circuit(self) -> None:
        self.store.append_history(
            error_id="e1", fingerprint="fp1", tool="email", attempt=1,
            tier=Tier.RULES, plan=make_plan(), outcome="failed",
        )
        self.store.write_circuit("email", 3, 100.0, 200.0)
        state = self.store.read_circuit("email")
        self.assertIsNotNone(state)
        self.assertEqual(state["failure_count"], 3)
        self.store.clear_circuit("email")
        self.assertIsNone(self.store.read_circuit("email"))

    def test_db_created(self) -> None:
        plan = make_plan()
        self.store.record_success("fp1", plan)
        self.assertTrue(Path(self.store._path).exists())


if __name__ == "__main__":
    unittest.main()