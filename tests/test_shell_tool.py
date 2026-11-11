"""End-to-end tests for the permission-checked shell tool."""

from __future__ import annotations

import time
import unittest
from unittest import mock

from controller import AuditError, SecureAuditLog

from tests.support import EnvMixin, read_jsonl
from tools.shell_tool import LAUNCHER_TIMEOUT, ShellTool


class ShellToolTests(EnvMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = ShellTool(config=self.config, default_cwd=self.work)
        self.audit_path = self.config.audit_root / "shell.jsonl"

    def _events(self) -> list[dict]:
        return read_jsonl(self.audit_path)

    async def test_allowed_command_runs_and_is_audited(self) -> None:
        result = await self.tool.execute("echo hello")
        self.assertIn("hello", result)
        self.assertIn("Exit code: 0", result)

        events = [event["event"] for event in self._events()]
        self.assertIn("shell_command_requested", events)
        self.assertIn("shell_command_completed", events)

        ok, count, message = SecureAuditLog(self.audit_path, fsync=False).verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, len(self._events()))

    async def test_working_directory_persists(self) -> None:
        result = await self.tool.execute("cd /tmp")
        self.assertIn("Working directory is now", result)
        self.assertEqual(str(self.tool.cwd), "/tmp")

        result = await self.tool.execute("pwd")
        self.assertIn("/tmp", result)

    async def test_denied_command_never_runs(self) -> None:
        result = await self.tool.execute("sudo ls")
        self.assertIn("Refused", result)
        events = [event["event"] for event in self._events()]
        self.assertNotIn("shell_command_requested", events)
        self.assertIn("shell_command_denied", events)

    async def test_sensitive_command_needs_single_use_token(self) -> None:
        target = self.work / "remove-me.txt"
        target.write_text("x", encoding="utf-8")
        command = f"rm {target}"

        first = await self.tool.execute(command)
        self.assertIn("Approval required", first)
        self.assertTrue(target.exists())
        token = first.split('approval_token="', 1)[1].split('"', 1)[0]

        second = await self.tool.execute(command, approval_token=token)
        self.assertIn("Exit code: 0", second)
        self.assertFalse(target.exists())

        third = await self.tool.execute(command, approval_token=token)
        self.assertIn("Refused", third)

    async def test_token_is_bound_to_the_command(self) -> None:
        first_file = self.work / "first.txt"
        second_file = self.work / "second.txt"
        first_file.write_text("a", encoding="utf-8")
        second_file.write_text("b", encoding="utf-8")

        first = await self.tool.execute(f"rm {first_file}")
        token = first.split('approval_token="', 1)[1].split('"', 1)[0]

        result = await self.tool.execute(
            f"rm {second_file}", approval_token=token
        )
        self.assertIn("Refused", result)
        self.assertTrue(second_file.exists())

    async def test_invalid_token_is_rejected(self) -> None:
        result = await self.tool.execute(
            "rm /tmp/alex-shell-never-existed", approval_token="deadbeef"
        )
        self.assertIn("Refused", result)

    async def test_timeout_kills_the_process_group(self) -> None:
        started = time.monotonic()
        result = await self.tool.execute("sleep 5", timeout=1)
        elapsed = time.monotonic() - started
        self.assertIn("Timed out after 1s", result)
        self.assertLess(elapsed, 4)

    async def test_output_is_capped(self) -> None:
        self.tool.max_output_chars = 50
        result = await self.tool.execute("ls /usr/bin")
        self.assertIn("output truncated", result)

    async def test_audit_failure_prevents_execution(self) -> None:
        broken = mock.Mock()
        broken.write.side_effect = AuditError("down")
        tool = ShellTool(
            config=self.config, audit=broken, default_cwd=self.work
        )
        result = await tool.execute("echo should-not-run")
        self.assertIn("audit log is unavailable", result)
        self.assertNotIn("Exit code", result)
        broken.write.assert_called()

    async def test_trusted_mode_runs_unknown_but_not_denied(self) -> None:
        tool = ShellTool(
            config=self.config, default_cwd=self.work, mode="trusted"
        )
        result = await tool.execute("definitely-not-a-real-program-alex")
        self.assertNotIn("Approval required", result)
        self.assertIn("Exit code: 127", result)

        result = await tool.execute("sudo ls")
        self.assertIn("Refused", result)

    async def test_audit_redacts_secret_assignments(self) -> None:
        await self.tool.execute("API_KEY=supersecretvalue ls")
        text = self.audit_path.read_text(encoding="utf-8")
        self.assertNotIn("supersecretvalue", text)
        self.assertIn("[redacted]", text)

    async def test_user_authorized_command_runs_without_a_token(self) -> None:
        target = self.work / "authorized-remove.txt"
        target.write_text("x", encoding="utf-8")

        result = await self.tool.execute(f"rm {target}", authorized=True)
        self.assertNotIn("Approval required", result)
        self.assertIn("Exit code: 0", result)
        self.assertFalse(target.exists())

        requested = [
            event
            for event in self._events()
            if event["event"] == "shell_command_requested"
        ]
        self.assertTrue(requested[-1]["authorized"])

    async def test_authorization_cannot_run_a_denied_command(self) -> None:
        result = await self.tool.execute("sudo ls", authorized=True)
        self.assertIn("Refused", result)
        events = [event["event"] for event in self._events()]
        self.assertIn("shell_command_denied", events)
        self.assertNotIn("shell_command_requested", events)

    def test_gui_launcher_gets_a_short_default_timeout(self) -> None:
        self.assertEqual(
            self.tool._effective_timeout("xdg-open https://example.com", None),
            LAUNCHER_TIMEOUT,
        )
        self.assertEqual(
            self.tool._effective_timeout("gio open https://example.com", None),
            LAUNCHER_TIMEOUT,
        )
        self.assertEqual(
            self.tool._effective_timeout("gio trash old.txt", None),
            self.tool.default_timeout,
        )
        self.assertEqual(
            self.tool._effective_timeout("ls -la", None),
            self.tool.default_timeout,
        )
        # An explicit caller timeout is honoured, even for a launcher.
        self.assertEqual(
            self.tool._effective_timeout("xdg-open https://example.com", 45), 45
        )
