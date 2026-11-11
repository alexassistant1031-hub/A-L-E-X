"""Regression tests for the recovery fixes.

Covers:
* persistent memory reaches the system prompt and the policy is never trimmed,
* the reach tool is inside the recovery boundary,
* OpenCode plans cannot inject authorization flags or unknown kwargs,
* a plan that asks for confirmation is escalated, never executed,
* OpenCode is called through its async client on the voice path.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path

from recovery import executor as exec_mod
from recovery.error_manager import ErrorManager
from recovery.executor import _accepted_kwargs
from recovery.opencode_client import OpenCodeClient
from recovery.playbook_store import PlaybookStore
from recovery.recovery_models import (
    ErrorCategory,
    ErrorReport,
    OperationMeta,
    RecoveryPlan,
    RecoveryResult,
    SafetyTag,
    Tier,
)
from recovery.retry_manager import RetryManager


class PromptAssemblyTests(unittest.TestCase):
    """`compose_instructions` must keep memory *and* the full policy."""

    POLICY = (
        "CORE POLICY ...\nEMAIL:\n- rules\nGITHUB:\n- rules\n"
        "EMERGENCY / CRITICAL MODE:\n- stay calm\n\nTOOL FAILURE RECOVERY\n"
    )

    def _compose(self, memory: str) -> str:
        from prompt_budget import compose_instructions

        return compose_instructions(
            self.POLICY, memory, max_instructions=32000, max_memory_chars=10000
        )

    def test_memory_reaches_the_prompt(self) -> None:
        prompt = self._compose("MEMORY-MARKER: user likes tea")
        self.assertIn("MEMORY-MARKER: user likes tea", prompt)
        self.assertIn("EMERGENCY / CRITICAL MODE", prompt)
        self.assertIn("TOOL FAILURE RECOVERY", prompt)

    def test_oversized_memory_is_trimmed_and_policy_survives(self) -> None:
        prompt = self._compose("x" * 50000)
        self.assertLessEqual(len(prompt), 32000)
        self.assertIn("EMERGENCY / CRITICAL MODE", prompt)
        self.assertIn("TOOL FAILURE RECOVERY", prompt)
        self.assertIn("memory trimmed", prompt)

    def test_policy_alone_too_large_raises(self) -> None:
        from prompt_budget import compose_instructions

        with self.assertRaises(RuntimeError):
            compose_instructions(
                "y" * 100, "", max_instructions=50, max_memory_chars=10
            )

    def test_main_wires_memory_into_the_agent(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        self.assertIn("agent = AlexAgent(instructions=instructions)", source)
        self.assertIn(
            'f"{ALEX_INSTRUCTIONS}\\n\\n{RECOVERY_INSTRUCTION}"', source
        )


class FakeManager:
    """Minimal manager that records the report and can recover."""

    def __init__(self, result: RecoveryResult | None = None) -> None:
        self.enabled = True
        self.called = False
        self.report = None
        self.result = result or RecoveryResult(recovered=True, result="recovered")

    async def handle_async(self, report, *, retry, meta):
        self.called = True
        self.report = report
        return self.result


class _Viz:
    def task(self, *args, **kwargs):
        return contextlib.nullcontext()


class ReachBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """The reach tool must enter the same recovery boundary as every other tool."""

    async def asyncSetUp(self) -> None:
        from tools import reach_tool

        self.mod = reach_tool
        self._orig_manager = exec_mod._manager
        self._orig_get_reach = reach_tool._get_reach
        self._orig_viz = reach_tool.get_visualizer

    async def asyncTearDown(self) -> None:
        exec_mod._manager = self._orig_manager
        self.mod._get_reach = self._orig_get_reach
        self.mod.get_visualizer = self._orig_viz

    async def test_reach_exception_enters_recovery(self) -> None:
        class Boom:
            async def execute(self, **kwargs):
                raise RuntimeError("reach exploded")

        manager = FakeManager()
        exec_mod._manager = manager
        self.mod._get_reach = lambda: Boom()
        self.mod.get_visualizer = lambda: _Viz()

        result = await self.mod.reach._func(action="github_read", target="owner/repo")
        self.assertTrue(manager.called)
        self.assertEqual(manager.report.tool, "reach")
        self.assertEqual(result, "recovered")

    async def test_reach_error_string_enters_recovery(self) -> None:
        class Rejected:
            async def execute(self, **kwargs):
                return "Agent Reach request rejected: bad action"

        manager = FakeManager()
        exec_mod._manager = manager
        self.mod._get_reach = lambda: Rejected()
        self.mod.get_visualizer = lambda: _Viz()

        await self.mod.reach._func(action="github_read")
        self.assertTrue(manager.called)
        self.assertEqual(manager.report.tool, "reach")


class OpencodeArgsTests(unittest.IsolatedAsyncioTestCase):
    """Plans may refine arguments but never authorization flags or stray keys."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="alex-recovery-fix-")
        self.addCleanup(self._tmp.cleanup)
        self.store = PlaybookStore(Path(self._tmp.name) / "recovery.db")
        self.manager = ErrorManager(
            enabled=True,
            max_auto_attempts=3,
            backoff_base=0.0,
            store=self.store,
            opencode=_UnavailableOpencode(),
        )
        self.retry_mgr = RetryManager(max_attempts=3, backoff_base=0.0)

    async def test_unknown_plan_kwargs_are_filtered(self) -> None:
        calls = {"n": 0}

        async def func(to: str = "") -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first attempt fails")
            return f"to={to}"

        self.assertEqual(
            _accepted_kwargs(func, {"to": "a", "_opencode_elapsed": 1.2}),
            {"to": "a"},
        )

        class RetryManagerDouble:
            enabled = True

            async def handle_async(self, report, *, retry, meta):
                result = await retry({"to": "a", "_opencode_elapsed": 1.2})
                return RecoveryResult(recovered=True, result=result)

        previous = exec_mod._manager
        exec_mod._manager = RetryManagerDouble()
        try:
            result = await exec_mod.execute_tool(
                tool="reach",
                operation="github_read",
                func=func,
            )
        finally:
            exec_mod._manager = previous
        self.assertEqual(result, "to=a")

    async def test_plan_cannot_set_authorization_flags(self) -> None:
        captured: dict = {}

        async def retry(args):
            captured.update(args)
            return "ok"

        plan = RecoveryPlan(
            error_id="e1", diagnosis="d", category=ErrorCategory.ENVIRONMENT,
            action="retry", args={"to": "new@example.com", "confirmed": True},
            risk_level="low", requires_confirmation=False, retry_allowed=True,
            reason="r", tier=Tier.OPENCODE, source="opencode",
        )
        meta = OperationMeta(tool="github", operation="issue_create")
        original = {"to": "old@example.com", "confirmed": False}
        report = ErrorReport.new(
            tool="github", operation="issue_create",
            error=Exception("boom"), args=original,
        )

        outcome = await self.manager._execute_plan(
            plan, retry, dict(original), meta, report, [], self.retry_mgr
        )
        self.assertTrue(outcome["recovered"])
        self.assertEqual(captured["to"], "new@example.com")
        self.assertIs(captured["confirmed"], False)


class _UnavailableOpencode:
    def get_plan(self, payload):
        return "unavailable", None

    async def get_plan_async(self, payload):
        return "unavailable", None


class _PendingConfirmationOpencode:
    def __init__(self) -> None:
        self.plan = RecoveryPlan(
            error_id="e1", diagnosis="needs a yes",
            category=ErrorCategory.ENVIRONMENT, action="retry",
            args={}, risk_level="low", requires_confirmation=True,
            retry_allowed=True, reason="needs your confirmation",
            tier=Tier.OPENCODE, source="opencode",
        )

    def get_plan(self, payload):
        return "ok", self.plan


class ConfirmationGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_flagging_confirmation_is_escalated(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="alex-recovery-gate-")
        self.addCleanup(tmp.cleanup)
        manager = ErrorManager(
            enabled=True, max_auto_attempts=3, backoff_base=0.0,
            store=PlaybookStore(Path(tmp.name) / "recovery.db"),
            opencode=_PendingConfirmationOpencode(),
        )
        calls = []

        async def retry(args):
            calls.append(args)
            return "should not run"

        report = ErrorReport.new(
            tool="shell", operation="run",
            error=Exception("command not found"), args={},
        )
        result = await manager.handle_async(
            report, retry=retry,
            meta=OperationMeta(tool="shell", operation="run"),
        )
        self.assertFalse(result.recovered)
        self.assertTrue(result.escalated)
        self.assertEqual(calls, [])


class AsyncOpencodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_awaits_the_async_client(self) -> None:
        class AsyncClient:
            def __init__(self) -> None:
                self.called = False

            async def get_plan_async(self, payload):
                self.called = True
                return "unavailable", None

        tmp = tempfile.TemporaryDirectory(prefix="alex-recovery-async-")
        self.addCleanup(tmp.cleanup)
        client = AsyncClient()
        manager = ErrorManager(
            enabled=True, max_auto_attempts=3, backoff_base=0.0,
            store=PlaybookStore(Path(tmp.name) / "recovery.db"),
            opencode=client,
        )

        async def retry(args):
            raise RuntimeError("still broken")

        report = ErrorReport.new(
            tool="shell", operation="run",
            error=Exception("command not found"), args={},
        )
        result = await manager.handle_async(
            report, retry=retry,
            meta=OperationMeta(tool="shell", operation="run"),
        )
        self.assertTrue(client.called)
        self.assertFalse(result.recovered)

    async def test_async_client_without_url_is_unavailable(self) -> None:
        client = OpenCodeClient(base_url="")
        status, plan = await client.get_plan_async({"error_id": "e1"})
        self.assertEqual(status, "unavailable")
        self.assertIsNone(plan)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
