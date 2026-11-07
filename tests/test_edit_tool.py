"""End-to-end tests for the exact-string edit tool."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from controller import AuditError, BackupManager, SecureAuditLog

from tests.support import EnvMixin, read_jsonl
from tools.edit_tool import EditTool


class EditToolTests(EnvMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = EditTool(config=self.config)
        self.audit_path = self.config.audit_root / "file.jsonl"
        self.target = self.work / "notes.txt"
        self.target.write_text(
            "alpha\nbeta\ngamma\nbeta\n", encoding="utf-8"
        )

    def _events(self) -> list[dict]:
        return read_jsonl(self.audit_path)

    async def test_exact_edit_creates_backup_and_audit(self) -> None:
        result = await self.tool.execute(str(self.target), "alpha", "ALPHA")
        self.assertIn("replaced 1 occurrence", result)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "ALPHA\nbeta\ngamma\nbeta\n",
        )

        archives = list((self.config.backup_root / "paths").glob("*.tar.gz"))
        self.assertTrue(archives, "a path snapshot of the edited file is required")
        self.assertIn("Backup:", result)
        events = [event["event"] for event in self._events()]
        self.assertIn("file_edit_requested", events)
        self.assertIn("safety_snapshot", events)
        self.assertIn("file_edit_completed", events)

        ok, count, message = SecureAuditLog(self.audit_path, fsync=False).verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, len(self._events()))

    async def test_ambiguous_match_is_refused(self) -> None:
        result = await self.tool.execute(str(self.target), "beta", "BETA")
        self.assertIn("appears 2 times", result)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "alpha\nbeta\ngamma\nbeta\n",
        )
        self.assertEqual(BackupManager(self.config).list_backups(), [])

    async def test_replace_all(self) -> None:
        result = await self.tool.execute(
            str(self.target), "beta", "BETA", replace_all=True
        )
        self.assertIn("replaced 2 occurrences", result)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "alpha\nBETA\ngamma\nBETA\n",
        )

    async def test_core_file_is_denied_without_backup(self) -> None:
        core = self.project / "core.py"
        result = await self.tool.execute(str(core), "ORIGINAL", "HACKED")
        self.assertIn("Permission denied", result)
        self.assertEqual(core.read_text(encoding="utf-8"), "ORIGINAL\n")
        self.assertEqual(BackupManager(self.config).list_backups(), [])

    async def test_hardlinked_file_is_refused(self) -> None:
        other = self.work / "other.txt"
        os.link(self.target, other)
        result = await self.tool.execute(str(self.target), "alpha", "ALPHA")
        self.assertIn("hard links", result)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "alpha\nbeta\ngamma\nbeta\n",
        )

    async def test_binary_file_is_refused(self) -> None:
        binary = self.work / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02")
        result = await self.tool.execute(str(binary), "\x00", "x")
        self.assertIn("binary", result)

    async def test_empty_old_string_is_refused(self) -> None:
        result = await self.tool.execute(str(self.target), "", "x")
        self.assertIn("must not be empty", result)

    async def test_no_match_is_refused(self) -> None:
        result = await self.tool.execute(str(self.target), "missing", "x")
        self.assertIn("No match", result)

    async def test_identical_strings_are_a_no_op(self) -> None:
        result = await self.tool.execute(str(self.target), "alpha", "alpha")
        self.assertIn("No change", result)

    async def test_crlf_line_endings_are_preserved(self) -> None:
        crlf = self.work / "crlf.txt"
        crlf.write_bytes(b"one\r\ntwo\r\n")
        result = await self.tool.execute(str(crlf), "one", "ONE")
        self.assertIn("Edited", result)
        self.assertEqual(crlf.read_bytes(), b"ONE\r\ntwo\r\n")

    async def test_audit_failure_prevents_the_edit(self) -> None:
        broken = mock.Mock()
        broken.write.side_effect = AuditError("down")
        tool = EditTool(config=self.config, audit=broken)
        result = await tool.execute(str(self.target), "alpha", "ALPHA")
        self.assertIn("Permission denied", result)
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "alpha\nbeta\ngamma\nbeta\n",
        )
        broken.write.assert_called()

    async def test_staging_edit_takes_project_backup(self) -> None:
        staged = self.staging / "draft.py"
        staged.write_text("value = 1\n", encoding="utf-8")
        result = await self.tool.execute(str(staged), "1", "2")
        self.assertIn("Edited", result)
        self.assertEqual(staged.read_text(encoding="utf-8"), "value = 2\n")
        self.assertEqual(len(BackupManager(self.config).list_backups()), 1)
