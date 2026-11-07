"""Tests for controller status reporting."""

from __future__ import annotations

import unittest
from dataclasses import replace

from controller import controller_status, describe_status

from tests.support import TempEnvTestCase


class StatusTests(TempEnvTestCase):
    def test_ready_environment(self) -> None:
        status = controller_status(self.config)
        self.assertTrue(status["ready"], status["message"])
        self.assertEqual(status["backup_root"], str(self.config.backup_root))
        self.assertEqual(status["audit_root"], str(self.config.audit_root))
        text = describe_status(status)
        self.assertIn("ready:    yes", text)
        self.assertIn(str(self.config.staging_root), text)

    def test_unusable_audit_root_is_reported(self) -> None:
        bad = self.base / "audit-file"
        bad.write_text("x", encoding="utf-8")
        config = replace(self.config, audit_root=bad)
        status = controller_status(config)
        self.assertFalse(status["ready"])
        self.assertIn("audit", status["message"])
        self.assertIn("NO", describe_status(status))


if __name__ == "__main__":
    unittest.main()
