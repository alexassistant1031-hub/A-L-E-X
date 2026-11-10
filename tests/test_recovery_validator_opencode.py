"""Tests for the plan validator and OpenCode client."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller import WritePathGuard, get_config, reset_config

from recovery.opencode_client import OpenCodeClient
from recovery.plan_validator import validate_plan
from recovery.recovery_models import (
    ErrorCategory, RecoveryPlan, Tier,
)
from tests.support import TempEnvTestCase


def make_plan(**overrides) -> RecoveryPlan:
    data = dict(
        error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
        action="retry", args={}, risk_level="low",
        requires_confirmation=False, retry_allowed=True, reason="r",
        tier=Tier.RULES, source="rules",
    )
    data.update(overrides)
    return RecoveryPlan(**data)


class ValidatorTests(TempEnvTestCase, unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        ok, reason = validate_plan(make_plan(action="retry"))
        self.assertTrue(ok, reason)

    def test_unknown_action_rejected(self) -> None:
        ok, reason = validate_plan(make_plan(action="delete_everything"))
        self.assertFalse(ok)

    def test_auth_error_cannot_be_auto_repaired(self) -> None:
        ok, reason = validate_plan(
            make_plan(category=ErrorCategory.AUTH, action="retry"))
        self.assertFalse(ok)

    def test_permission_error_cannot_be_auto_repaired(self) -> None:
        ok, reason = validate_plan(
            make_plan(category=ErrorCategory.PERMISSION, action="retry"))
        self.assertFalse(ok)

    def test_secret_exposure_rejected(self) -> None:
        ok, reason = validate_plan(make_plan(
            diagnosis="use token ghp_abcdefghijklmnopqrstuvwxyz to fix"))
        self.assertFalse(ok)

    def test_guard_allows_staging_path(self) -> None:
        guard = WritePathGuard(self.config)
        ok, reason = validate_plan(make_plan(
            action="retry_with_args", args={"path": str(self.staging / "note.txt")}),
            guard=guard)
        self.assertTrue(ok, reason)

    def test_guard_rejects_protected_path(self) -> None:
        guard = WritePathGuard(self.config)
        ok, reason = validate_plan(make_plan(
            action="retry_with_args", args={"path": str(self.project / "core.py")}),
            guard=guard)
        self.assertFalse(ok, reason)


class OpencodeClientTests(unittest.TestCase):
    def test_no_url_returns_unavailable(self) -> None:
        client = OpenCodeClient(base_url="")
        status, plan = client.get_plan({"error_id": "e1"})
        self.assertEqual(status, "unavailable")
        self.assertIsNone(plan)
        client.close()

    def test_invalid_json_rejected(self) -> None:
        client = OpenCodeClient(base_url="http://127.0.0.1:1")
        status, plan = client.get_plan({"error_id": "e1"})
        # Connection refused -> unavailable, never raises.
        self.assertEqual(status, "unavailable")
        self.assertIsNone(plan)
        client.close()


if __name__ == "__main__":
    unittest.main()