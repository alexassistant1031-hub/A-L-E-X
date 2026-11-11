"""Tests for the standing authorization policy.

The operator pre-authorizes tool actions on this dedicated machine, so the
execution boundary sets ``confirmed`` / ``authorized`` automatically.  Hard
refusals must remain untouched.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from authorization import apply_auto_authorization, auto_confirm_enabled
from recovery import executor as exec_mod
from tools.shell.permissions import ALLOW, DENY, analyze


class PolicyTests(unittest.TestCase):
    def test_enabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALEX_AUTO_CONFIRM", None)
            self.assertTrue(auto_confirm_enabled())

    def test_can_be_disabled(self) -> None:
        with patch.dict(os.environ, {"ALEX_AUTO_CONFIRM": "0"}):
            self.assertFalse(auto_confirm_enabled())

    def test_sets_flags_the_callable_accepts(self) -> None:
        def tool(confirmed: bool = False, authorized: bool = False) -> None:
            pass

        out = apply_auto_authorization(tool, {})
        self.assertIs(out["confirmed"], True)
        self.assertIs(out["authorized"], True)

    def test_does_not_add_unknown_flags(self) -> None:
        def tool(to: str = "") -> None:
            pass

        out = apply_auto_authorization(tool, {"to": "a@b.c"})
        self.assertEqual(out, {"to": "a@b.c"})

    def test_disabled_policy_leaves_args_untouched(self) -> None:
        def tool(confirmed: bool = False) -> None:
            pass

        with patch.dict(os.environ, {"ALEX_AUTO_CONFIRM": "0"}):
            self.assertEqual(apply_auto_authorization(tool, {}), {})


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._orig = exec_mod._manager

    async def asyncTearDown(self) -> None:
        exec_mod._manager = self._orig

    async def test_boundary_authorizes_the_call(self) -> None:
        exec_mod._manager = None

        async def send(to: str = "", confirmed: bool = False) -> str:
            return "sent" if confirmed else "blocked"

        with patch.dict(os.environ, {"ALEX_AUTO_CONFIRM": "1"}):
            result = await exec_mod.execute_tool(
                tool="email", operation="send", func=send, to="a@b.c"
            )
        self.assertEqual(result, "sent")

    async def test_disabled_policy_keeps_the_gate(self) -> None:
        exec_mod._manager = None

        async def send(to: str = "", confirmed: bool = False) -> str:
            return "sent" if confirmed else "blocked"

        with patch.dict(os.environ, {"ALEX_AUTO_CONFIRM": "0"}):
            result = await exec_mod.execute_tool(
                tool="email", operation="send", func=send, to="a@b.c"
            )
        self.assertEqual(result, "blocked")


class HardRefusalTests(unittest.TestCase):
    def test_authorization_never_overrides_deny(self) -> None:
        self.assertEqual(analyze("sudo rm -rf /", authorized=True).tier, DENY)
        self.assertEqual(analyze("cat .env.local", authorized=True).tier, DENY)

    def test_authorization_allows_ask_tier(self) -> None:
        self.assertEqual(
            analyze("rm -rf /tmp/alex-test", authorized=True).tier, ALLOW
        )


class PromptPolicyTests(unittest.TestCase):
    def test_prompt_states_the_standing_authorization(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()
        self.assertIn("full standing authorization", source)
        self.assertIn("Never ask", source)
        self.assertNotIn("ask once first", source)
        self.assertNotIn("needs one question first", source)


if __name__ == "__main__":
    unittest.main()
