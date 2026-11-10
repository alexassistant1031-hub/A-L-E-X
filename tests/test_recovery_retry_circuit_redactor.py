"""Tests for retry manager, circuit breaker and redactor."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from recovery.circuit_breaker import CircuitBreaker
from recovery.redactor import redact_report, redact
from recovery.recovery_models import ErrorCategory
from recovery.retry_manager import RetryManager


class RetryManagerTests(unittest.TestCase):
    def test_stops_at_retry_limit(self) -> None:
        mgr = RetryManager(max_attempts=3)
        mgr.record("fp1", ErrorCategory.TRANSIENT, "tier_failed")   # attempt 1
        mgr.record("fp2", ErrorCategory.TRANSIENT, "tier_failed")   # attempt 2
        self.assertFalse(mgr.should_stop(ErrorCategory.TRANSIENT, "fp3")[0])
        mgr.record("fp3", ErrorCategory.TRANSIENT, "tier_failed")   # attempt 3
        stop, reason = mgr.should_stop(ErrorCategory.TRANSIENT, "fp4")
        self.assertTrue(stop)
        self.assertIn("retry limit", reason)

    def test_stops_on_repeated_identical_error(self) -> None:
        mgr = RetryManager(max_attempts=10)
        mgr.record("fp1", ErrorCategory.TRANSIENT, "tier_failed")
        mgr.record("fp1", ErrorCategory.TRANSIENT, "tier_failed")
        stop, _ = mgr.should_stop(ErrorCategory.TRANSIENT, "fp1")
        self.assertTrue(stop)

    def test_auth_and_permanent_stop_immediately(self) -> None:
        mgr = RetryManager(max_attempts=3)
        stop, _ = mgr.should_stop(ErrorCategory.AUTH, "fp1")
        self.assertTrue(stop)
        stop, _ = mgr.should_stop(ErrorCategory.PERMISSION, "fp1")
        self.assertTrue(stop)
        stop, _ = mgr.should_stop(ErrorCategory.PERMANENT, "fp1")
        self.assertTrue(stop)

    def test_backoff_grows(self) -> None:
        mgr = RetryManager(max_attempts=3, backoff_base=0.01)
        mgr.attempt = 1
        self.assertEqual(mgr.backoff_seconds(), 0.01)
        mgr.attempt = 2
        self.assertEqual(mgr.backoff_seconds(), 0.02)


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(threshold=2, cooldown=60.0)
        self.assertFalse(cb.is_open("email"))
        cb.record_failure("email")
        self.assertFalse(cb.is_open("email"))
        cb.record_failure("email")
        self.assertTrue(cb.is_open("email"))

    def test_success_resets(self) -> None:
        cb = CircuitBreaker(threshold=1, cooldown=60.0)
        cb.record_failure("email")
        self.assertTrue(cb.is_open("email"))
        cb.record_success("email")
        self.assertFalse(cb.is_open("email"))


class RedactorTests(unittest.TestCase):
    def test_strips_tokens_and_emails(self) -> None:
        text = "Token ghp_abcdefghijklmnopqrstuvwxyz and email user@example.com leaked"
        out = redact_report({"msg": text, "nested": {"k": "AIza1234567890123456789012345678901234567"}})
        self.assertNotIn("ghp_", out["msg"])
        self.assertNotIn("user@example.com", out["msg"])
        self.assertNotIn("AIza", out["nested"]["k"])
        self.assertIn("[GITHUB_TOKEN]", out["msg"])
        self.assertIn("[EMAIL]", out["msg"])
        self.assertIn("[GOOGLE_API_KEY]", out["nested"]["k"])

    def test_redacts_secret_keys(self) -> None:
        out = redact({"api_key": "supersecret123", "password": "hunter2", "safe": "ok"})
        self.assertEqual(out["api_key"], "[REDACTED]")
        self.assertEqual(out["password"], "[REDACTED]")
        self.assertEqual(out["safe"], "ok")


if __name__ == "__main__":
    unittest.main()