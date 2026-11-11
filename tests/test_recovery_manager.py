"""Tests for the ErrorManager tiered recovery pipeline."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controller import get_config, reset_config

from recovery.error_manager import ErrorManager, run_recovery
from recovery.error_classifier import ErrorCategory, NEVER_AUTO_BYPASS
from recovery.fingerprint import fingerprint
from recovery.opencode_client import OpenCodeClient
from recovery.playbook_store import PlaybookStore
from recovery.recovery_models import (
    ErrorReport, OperationMeta, RecoveryPlan, RecoveryResult, SafetyTag, Tier,
)
from tests.support import TempEnvTestCase


class FakeOpencode:
    """Stand-in OpenCode client: returns a configured (status, plan)."""

    def __init__(self, status="ok", plan=None) -> None:
        self.status = status
        self.plan = plan
        self.calls = 0

    def get_plan(self, report_json):
        self.calls += 1
        return self.status, self.plan


class FakeRetry:
    """Stand-in retry callable: raises N times then returns success."""

    def __init__(self, fail_times: int, success_result="recovered-ok") -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.success_result = success_result
        self.received_args = []

    async def __call__(self, args: dict):
        self.calls += 1
        self.received_args.append(dict(args))
        if self.calls <= self.fail_times:
            raise RuntimeError("transient failure")
        return self.success_result


def make_meta(tool="email", operation="send") -> OperationMeta:
    return OperationMeta(
        tool=tool, operation=operation, safety_tag=SafetyTag.READ,
        error_prefixes=("Email: ",),
    )


class ManagerTests(TempEnvTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.store = PlaybookStore(Path(self.base) / "recovery.db")

    def make_manager(self, *, opencode=None, enabled=True, max_attempts=3):
        return ErrorManager(
            enabled=enabled,
            max_auto_attempts=max_attempts,
            backoff_base=0.0,  # no real sleeping in tests
            circuit_threshold=5,
            circuit_cooldown=300.0,
            store=self.store,
            opencode=opencode or FakeOpencode(),
        )

    async def test_recovery_disabled_passes_through(self) -> None:
        manager = self.make_manager(enabled=False)
        report = ErrorReport.new(
            tool="email", operation="send",
            error=Exception("boom"), args={"to": "a@b.c"},
        )
        result = await manager.handle_async(
            report, retry=lambda args: asyncio.sleep(0) or "ok", meta=make_meta()
        )
        self.assertFalse(result.recovered)
        self.assertFalse(result.escalated)
    async def test_tier0_playbook_hit_retry_succeeds(self) -> None:
        fp = fingerprint("email", "send", ErrorCategory.TRANSIENT, "timeout")
        plan = RecoveryPlan(
            error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
            action="retry", args={}, risk_level="low",
            requires_confirmation=False, retry_allowed=True, reason="known",
            tier=Tier.PLAYBOOK, source="playbook",
        )
        self.store.record_success(fp, plan)
        retry = FakeRetry(fail_times=0)
        manager = self.make_manager(opencode=FakeOpencode(status="unavailable"))
        report = ErrorReport(
            error_id="e", tool="email", operation="send",
            error_type="Exception", normalized_message="timeout",
            raw_message="timeout", category=ErrorCategory.TRANSIENT,
            args={}, fingerprint=fp, created_at="now",
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta())
        self.assertTrue(result.recovered)
        self.assertEqual(result.tier_used, Tier.PLAYBOOK)
        self.assertEqual(retry.calls, 1)

    async def test_tier1_transient_retry_succeeds(self) -> None:
        retry = FakeRetry(fail_times=1)
        manager = self.make_manager(opencode=FakeOpencode(status="unavailable"))
        report = ErrorReport.new(
            tool="email", operation="send",
            error=Exception("read timeout"), args={},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta())
        self.assertTrue(result.recovered)
        self.assertEqual(result.tier_used, Tier.RULES)
        self.assertEqual(retry.calls, 2)

    async def test_opencode_valid_plan_retry_and_playbook_stored(self) -> None:
        fp = fingerprint("shell", "run", ErrorCategory.ENVIRONMENT, "command not found")
        plan = RecoveryPlan(
            error_id="e1", diagnosis="missing dependency",
            category=ErrorCategory.ENVIRONMENT, action="retry",
            args={}, risk_level="low", requires_confirmation=False,
            retry_allowed=True, reason="install dep then retry",
            tier=Tier.OPENCODE, source="opencode",
        )
        opencode = FakeOpencode(status="ok", plan=plan)
        retry = FakeRetry(fail_times=1)
        manager = self.make_manager(opencode=opencode)
        report = ErrorReport.new(
            tool="shell", operation="run",
            error=Exception("command not found"), args={},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta("shell", "run"))
        self.assertTrue(result.recovered)
        self.assertEqual(result.tier_used, Tier.OPENCODE)
        self.assertIsNotNone(self.store.lookup(fp))

    async def test_retry_limit_stop_with_full_report(self) -> None:
        retry = FakeRetry(fail_times=10)
        manager = self.make_manager(max_attempts=3, opencode=FakeOpencode(status="unavailable"))
        report = ErrorReport.new(
            tool="email", operation="send",
            error=Exception("read timeout"), args={"to": "a@b.c"},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta())
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)
        self.assertGreaterEqual(result.attempts, 1)
        self.assertIn("Recovery exhausted", result.final_message)
        self.assertIn("Original:", result.final_message)
        self.assertIn("Latest:", result.final_message)

    async def test_opencode_unavailable_graceful_fallback(self) -> None:
        retry = FakeRetry(fail_times=10)
        manager = self.make_manager(max_attempts=2, opencode=FakeOpencode(status="unavailable"))
        report = ErrorReport.new(
            tool="email", operation="send",
            error=Exception("read timeout"), args={},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta())
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)

    async def test_repeated_identical_error_stopped(self) -> None:
        retry = FakeRetry(fail_times=10)
        manager = self.make_manager(max_attempts=10, opencode=FakeOpencode(status="unavailable"))
        report = ErrorReport.new(
            tool="email", operation="send",
            error=Exception("read timeout"), args={},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta())
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)
        self.assertTrue(result.attempts <= manager.max_auto_attempts)

    async def test_auth_error_escalates_never_auto_bypassed(self) -> None:
        retry = FakeRetry(fail_times=0)
        manager = self.make_manager(opencode=FakeOpencode(status="ok"))
        report = ErrorReport.new(
            tool="github", operation="file_write",
            error=Exception("401 unauthorized"), args={},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta("github", "file_write"))
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)
        self.assertEqual(retry.calls, 0)

    async def test_permission_error_escalates_never_auto_bypassed(self) -> None:
        retry = FakeRetry(fail_times=0)
        manager = self.make_manager(opencode=FakeOpencode(status="ok"))
        report = ErrorReport.new(
            tool="file_operation", operation="write",
            error=Exception("permission denied"), args={"path": "/etc/passwd"},
        )
        result = await manager.handle_async(report, retry=retry, meta=make_meta("file_operation", "write"))
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)
        self.assertEqual(retry.calls, 0)


if __name__ == "__main__":
    unittest.main()
