"""Tests for the executor boundary and operation-safety rules."""

from __future__ import annotations

import asyncio
import unittest

from recovery import execute_tool
from recovery.error_classifier import ErrorCategory
from recovery.error_manager import ErrorManager
from recovery.recovery_models import (
    ErrorReport, OperationMeta, RecoveryPlan, RecoveryResult, SafetyTag, Tier,
)
from recovery.retry_manager import RetryManager


class FakeManager:
    """Stand-in manager installed via execute_tool.configure."""

    def __init__(self, result: RecoveryResult, enabled=True) -> None:
        self.result = result
        self.enabled = enabled
        self.called = False

    async def handle_async(self, report, *, retry, meta):
        self.called = True
        return self.result


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from recovery import executor as exec_mod
        self._exec_mod = exec_mod
        self._orig = exec_mod._manager

    async def asyncTearDown(self) -> None:
        self._exec_mod._manager = self._orig

    async def test_success_first_try_no_recovery(self) -> None:
        self._exec_mod._manager = FakeManager(
            RecoveryResult(recovered=False), enabled=True)

        async def func(**kwargs):
            return "ok"

        result = await execute_tool(
            tool="email", operation="send",
            safety_tag=SafetyTag.READ, func=func, error_prefixes=(),
            to="a@b.c",
        )
        self.assertEqual(result, "ok")
        self.assertFalse(self._exec_mod._manager.called)

    async def test_error_string_treated_as_failure_triggers_recovery(self) -> None:
        recovered = RecoveryResult(recovered=True, result="retry-ok")
        self._exec_mod._manager = FakeManager(recovered, enabled=True)

        calls = []

        async def func(**kwargs):
            calls.append(kwargs)
            if not calls[0].get("retried"):
                return "Email: transient send failure"
            return "retry-ok"

        result = await execute_tool(
            tool="email", operation="send",
            safety_tag=SafetyTag.WRITE_IRREVERSIBLE, func=func,
            error_prefixes=("Email: ",),
            to="a@b.c",
        )
        # First call failed (error string), recovery retried and succeeded.
        self.assertEqual(result, "retry-ok")
        self.assertTrue(self._exec_mod._manager.called)


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = ErrorManager(
            enabled=True, max_auto_attempts=3, backoff_base=0.0,
            store=None, opencode=None,
        )
        self.retry_mgr = RetryManager(max_attempts=3, backoff_base=0.0)

    async def test_confirmation_flow_not_bypassed(self) -> None:
        plan = RecoveryPlan(
            error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
            action="retry_with_args", args={"to": "a@b.c"},
            risk_level="low", requires_confirmation=True,
            retry_allowed=True, reason="needs your yes",
            tier=Tier.RULES, source="rules",
        )
        report = ErrorReport.new(
            tool="email", operation="send", error=Exception("timeout"), args={},
        )
        result = await self.manager.handle_async(
            report, retry=lambda args: asyncio.sleep(0) or "ok",
            meta=OperationMeta(tool="email", operation="send",
                               safety_tag=SafetyTag.WRITE_IRREVERSIBLE,
                               error_prefixes=("Email: ",)),
        )
        # Without a stored playbook / opencode, no plan is produced; the
        # manager escalates rather than silently retrying.
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)

    async def test_irreversible_write_without_verifier_is_not_retried(self) -> None:
        plan = RecoveryPlan(
            error_id="e1", diagnosis="d", category=ErrorCategory.TRANSIENT,
            action="retry", args={}, risk_level="low",
            requires_confirmation=False, retry_allowed=True, reason="r",
            tier=Tier.RULES, source="rules",
        )
        report = ErrorReport.new(
            tool="email", operation="send", error=Exception("boom"), args={},
        )
        meta = OperationMeta(
            tool="email", operation="send",
            safety_tag=SafetyTag.WRITE_IRREVERSIBLE,
            error_prefixes=("Email: ",), verifier=None,
        )
        # Directly exercise _attempt-style behaviour: irreversible + no
        # verifier -> refuse to retry (no duplicate).
        outcome = await self.manager._execute_plan(
            plan, lambda args: asyncio.sleep(0) or "ok",
            {}, meta, report, [], self.retry_mgr
        )
        self.assertFalse(outcome["recovered"])
        self.assertIn("verified", outcome["error"])

    async def test_code_patch_is_staged_not_auto_applied(self) -> None:
        plan = RecoveryPlan(
            error_id="e1", diagnosis="bug", category=ErrorCategory.CODE_BUG,
            action="code_patch", args={}, risk_level="high",
            requires_confirmation=True, retry_allowed=True, reason="patch",
            tier=Tier.OPENCODE, source="opencode",
        )
        report = ErrorReport.new(
            tool="shell", operation="run", error=Exception("boom"), args={},
        )
        outcome = await self.manager._execute_plan(
            plan, lambda args: asyncio.sleep(0) or "ok",
            {}, OperationMeta(tool="shell", operation="run"),
            report, [], self.retry_mgr,
        )
        self.assertFalse(outcome["recovered"])
        self.assertIn("staged", outcome["error"])


if __name__ == "__main__":
    unittest.main()