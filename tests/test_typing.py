"""Tests for typing safety and its controller-owned audit log."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from controller import AuditError, SecureAuditLog

from tools.typing.safety import TypingGuard

from tests.support import TempEnvTestCase, read_jsonl


class TypingGuardTests(TempEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.audit_path = self.config.audit_root / "typing.jsonl"
        self.guard = TypingGuard(config=self.config)

    def test_record_is_written_to_secure_audit_log(self) -> None:
        self.guard.ensure_audit()
        self.guard.record("hello", "normal", "", "typed")

        events = read_jsonl(self.audit_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "typing_action")
        self.assertEqual(events[0]["text_preview"], "hello")
        self.assertEqual(events[0]["length"], 5)

        ok, count, message = SecureAuditLog(self.audit_path, fsync=False).verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 1)

    def test_destructive_text_is_blocked(self) -> None:
        with patch.dict(os.environ, {"ALEX_TYPING_ALLOW_DANGEROUS": "0"}):
            allowed, reason = self.guard.validate("rm -rf /")
        self.assertFalse(allowed)
        self.assertIn("destructive", reason)

    def test_length_limit_is_enforced(self) -> None:
        allowed, reason = self.guard.validate("x" * 2001)
        self.assertFalse(allowed)
        self.assertIn("limit", reason)

    def test_confirmation_for_long_text_and_clicks(self) -> None:
        self.assertTrue(self.guard.requires_confirmation("x" * 301))
        self.assertTrue(self.guard.requires_confirmation("short", click="10,20"))
        self.assertFalse(self.guard.requires_confirmation("short"))

    def test_audit_unavailable_fails_closed(self) -> None:
        bad_audit = self.base / "audit-is-a-file"
        bad_audit.write_text("x", encoding="utf-8")
        config = replace(self.config, audit_root=bad_audit)
        guard = TypingGuard(config=config)

        with self.assertRaises(AuditError):
            guard.ensure_audit()
        with self.assertRaises(AuditError):
            guard.record("hello", "normal", "", "typed")

    def test_rate_limit(self) -> None:
        guard = TypingGuard(audit=SecureAuditLog(self.audit_path, fsync=False))
        for _ in range(30):
            allowed, _reason = guard.validate("ok")
            self.assertTrue(allowed)
        allowed, reason = guard.validate("ok")
        self.assertFalse(allowed)
        self.assertIn("too many", reason)


if __name__ == "__main__":
    unittest.main()
