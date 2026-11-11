"""Tests for recovery models, error classifier and fingerprinting."""

from __future__ import annotations

import unittest

from recovery.error_classifier import NEVER_AUTO_BYPASS, classify
from recovery.fingerprint import fingerprint
from recovery.recovery_models import (
    ErrorCategory,
    ErrorReport,
    OperationMeta,
    RecoveryPlan,
    SafetyTag,
    Tier,
)
class ClassificationTests(unittest.TestCase):
    def test_auth_and_permission_never_auto_bypassed(self) -> None:
        self.assertIn(ErrorCategory.AUTH, NEVER_AUTO_BYPASS)
        self.assertIn(ErrorCategory.PERMISSION, NEVER_AUTO_BYPASS)

    def test_transient_classified(self) -> None:
        self.assertEqual(classify(Exception("HTTP 429 too many requests")), ErrorCategory.TRANSIENT)
        self.assertEqual(classify(Exception("read timeout")), ErrorCategory.TRANSIENT)
        self.assertEqual(classify(Exception("503 service unavailable")), ErrorCategory.TRANSIENT)

    def test_auth_classified(self) -> None:
        self.assertEqual(classify(Exception("401 unauthorized")), ErrorCategory.AUTH)
        self.assertEqual(classify(Exception("invalid credentials")), ErrorCategory.AUTH)

    def test_permission_classified(self) -> None:
        self.assertEqual(classify(Exception("permission denied")), ErrorCategory.PERMISSION)
        self.assertEqual(classify(Exception("access denied")), ErrorCategory.PERMISSION)

    def test_environment_classified(self) -> None:
        self.assertEqual(classify(ImportError("no module named foo")), ErrorCategory.ENVIRONMENT)
        self.assertEqual(classify(Exception("command not found")), ErrorCategory.ENVIRONMENT)

    def test_permanent_default(self) -> None:
        self.assertEqual(classify(Exception("no such file")), ErrorCategory.PERMANENT)
        self.assertEqual(classify(Exception("something weird happened")), ErrorCategory.PERMANENT)


class FingerprintTests(unittest.TestCase):
    def test_stable_across_variable_details(self) -> None:
        a = fingerprint("email", "send", ErrorCategory.TRANSIENT, "Connection reset by peer at /tmp/x/123 at 2024-01-01T00:00:00")
        b = fingerprint("email", "send", ErrorCategory.TRANSIENT, "Connection reset by peer at /var/log/y/999 at 2025-12-31T23:59:59")
        self.assertEqual(a, b)

    def test_different_errors_differ(self) -> None:
        a = fingerprint("email", "send", ErrorCategory.TRANSIENT, "timeout")
        b = fingerprint("email", "send", ErrorCategory.AUTH, "timeout")
        self.assertNotEqual(a, b)


class ModelTests(unittest.TestCase):
    def test_error_report_new(self) -> None:
        report = ErrorReport.new(
            tool="email", operation="send",
            error=Exception("boom"), args={"to": "a@b.c"},
        )
        self.assertEqual(report.tool, "email")
        self.assertEqual(report.operation, "send")
        self.assertEqual(report.category, ErrorCategory.PERMANENT)
        self.assertTrue(report.fingerprint)
        self.assertTrue(report.error_id)

    def test_operation_meta_defaults(self) -> None:
        meta = OperationMeta(tool="file", operation="read")
        self.assertEqual(meta.safety_tag, SafetyTag.READ)
        self.assertEqual(meta.error_prefixes, ())
        self.assertIsNone(meta.verifier)

    def test_recovery_plan_schema_validation(self) -> None:
        plan = RecoveryPlan(
            error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
            action="retry", args={}, risk_level="low",
            requires_confirmation=False, retry_allowed=True, reason="r",
            tier=Tier.RULES, source="rules",
        )
        self.assertTrue(plan.is_valid_schema())

    def test_recovery_plan_rejects_bad_action(self) -> None:
        plan = RecoveryPlan(
            error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
            action="not_an_action", args={}, risk_level="low",
            requires_confirmation=False, retry_allowed=True, reason="r",
            tier=Tier.RULES, source="rules",
        )
        self.assertFalse(plan.is_valid_schema())


if __name__ == "__main__":
    unittest.main()