"""End-to-end tests for the agent-facing FileTool.

These exercise the full path: policy check -> safety snapshot -> atomic
mutation -> controller-owned audit log.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from controller import BackupManager, SecureAuditLog

from tools.file_tool import FileTool

from tests.support import EnvMixin, read_jsonl


class FileToolTests(EnvMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = FileTool(config=self.config)
        self.audit_path = self.config.audit_root / "file.jsonl"

    def _events(self) -> list[dict]:
        return read_jsonl(self.audit_path)

    async def test_write_to_staging_creates_backup_and_audit(self) -> None:
        target = self.staging / "note.txt"
        result = await self.tool.execute(
            "write", str(target), content="hello"
        )

        self.assertIn("Written 5 bytes", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")

        backups = BackupManager(self.config).list_backups()
        self.assertEqual(len(backups), 1)

        events = [event["event"] for event in self._events()]
        self.assertIn("file_change_requested", events)
        self.assertIn("safety_snapshot", events)
        self.assertIn("file_change_completed", events)

        ok, count, message = SecureAuditLog(self.audit_path, fsync=False).verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, len(self._events()))

    async def test_write_to_core_is_denied_without_backup(self) -> None:
        target = self.project / "core.py"
        result = await self.tool.execute(
            "write", str(target), content="HACKED"
        )

        self.assertIn("Permission denied", result)
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL\n")
        self.assertEqual(BackupManager(self.config).list_backups(), [])

        events = [event["event"] for event in self._events()]
        self.assertIn("file_denied", events)

    async def test_read_of_retired_reply_path_redirects(self) -> None:
        old = self.base / "Desktop" / "reply.txt"
        new = self.base / "OpenCode Reply Text" / "reply.txt"
        with mock.patch(
            "settings.obsolete_reply_file", return_value=old
        ), mock.patch("settings.primary_reply_file", return_value=new):
            result = await self.tool.execute("read", str(old))
        self.assertIn("retired", result)
        self.assertIn(str(new), result)

    async def test_symlink_escape_is_denied(self) -> None:
        link = self.staging / "escape.py"
        link.symlink_to(self.project / "core.py")

        result = await self.tool.execute(
            "write", str(link), content="HACKED"
        )
        self.assertIn("Permission denied", result)
        self.assertEqual(
            (self.project / "core.py").read_text(encoding="utf-8"), "ORIGINAL\n"
        )

    async def test_append_to_working_root_snapshots_previous_content(self) -> None:
        document = self.work / "doc.txt"
        document.write_text("v1", encoding="utf-8")

        result = await self.tool.execute(
            "append", str(document), content="+v2"
        )
        self.assertIn("Appended 3 bytes", result)
        self.assertEqual(document.read_text(encoding="utf-8"), "v1+v2")

        manager = BackupManager(self.config)
        self.assertEqual(manager.list_backups(), [])
        snapshots = manager.list_path_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(manager.verify(snapshots[0]).ok)

        restored = self.base / "restored-append"
        manager.restore(snapshots[0], restored, confirm="RESTORE")
        self.assertEqual(
            (restored / "doc.txt").read_text(encoding="utf-8"), "v1"
        )

    async def test_delete_moves_to_trash_after_project_backup(self) -> None:
        target = self.staging / "trash-me.txt"
        target.write_text("bye", encoding="utf-8")

        result = await self.tool.execute("delete", str(target))
        self.assertIn("Moved file to trash", result)
        self.assertFalse(target.exists())
        self.assertTrue(any(self.config.trash_root.iterdir()))
        self.assertEqual(
            len(BackupManager(self.config).list_backups()), 1
        )

    async def test_delete_symlink_moves_link_not_target(self) -> None:
        target = self.work / "target.txt"
        target.write_text("keep me", encoding="utf-8")
        link = self.staging / "link.txt"
        link.symlink_to(target)

        result = await self.tool.execute("delete", str(link))
        self.assertIn("Moved", result)
        self.assertFalse(link.is_symlink())
        self.assertEqual(
            target.read_text(encoding="utf-8"), "keep me"
        )

        trashed = list(self.config.trash_root.iterdir())
        self.assertTrue(any(item.is_symlink() for item in trashed))

        # The link was inside the project, so a full project backup is taken
        # even though its target lives in a working directory.
        self.assertEqual(
            len(BackupManager(self.config).list_backups()), 1
        )

    async def test_mkdir_in_staging_is_snapshotted(self) -> None:
        target = self.staging / "newdir"
        result = await self.tool.execute("mkdir", str(target))
        self.assertIn("Created directory", result)
        self.assertTrue(target.is_dir())
        self.assertEqual(
            len(BackupManager(self.config).list_backups()), 1
        )

    async def test_copy_and_move_between_allowed_locations(self) -> None:
        source = self.staging / "source.txt"
        source.write_text("copy me", encoding="utf-8")

        copied = self.work / "copied.txt"
        result = await self.tool.execute(
            "copy", str(source), destination=str(copied)
        )
        self.assertIn("Copied", result)
        self.assertEqual(copied.read_text(encoding="utf-8"), "copy me")
        self.assertEqual(BackupManager(self.config).list_backups(), [])

        moved = self.work / "moved.txt"
        result = await self.tool.execute(
            "move", str(copied), destination=str(moved)
        )
        self.assertIn("Moved", result)
        self.assertFalse(copied.exists())
        self.assertEqual(moved.read_text(encoding="utf-8"), "copy me")

        manager = BackupManager(self.config)
        snapshots = manager.list_path_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertTrue(manager.verify(snapshots[0]).ok)

    async def test_each_mutation_gets_a_fresh_project_backup(self) -> None:
        config = replace(self.config, backup_coalesce_seconds=60)
        tool = FileTool(config=config)
        for index in range(2):
            target = config.staging_root / f"file-{index}.txt"
            result = await tool.execute("write", str(target), content="x")
            self.assertIn("Written 1 bytes", result)

        self.assertEqual(len(BackupManager(config).list_backups()), 2)

    async def test_read_only_actions_do_not_snapshot(self) -> None:
        result = await self.tool.execute("info", str(self.project / "core.py"))
        self.assertIn("Path:", result)
        result = await self.tool.execute("list", str(self.project))
        self.assertIn("Contents of", result)
        self.assertEqual(BackupManager(self.config).list_backups(), [])

    async def test_audit_failure_blocks_write(self) -> None:
        # A file where the audit directory should be makes the secure log
        # impossible to create.
        bad_audit = self.base / "not-a-directory"
        bad_audit.write_text("x", encoding="utf-8")
        config = replace(self.config, audit_root=bad_audit)
        tool = FileTool(config=config)

        target = config.staging_root / "blocked.txt"
        result = await tool.execute("write", str(target), content="nope")

        self.assertIn("Permission denied", result)
        self.assertIn("audit", result.lower())
        self.assertFalse(target.exists())
        self.assertEqual(BackupManager(config).list_backups(), [])

    async def test_delete_allowed_root_is_refused(self) -> None:
        result = await self.tool.execute("delete", str(self.work))
        self.assertIn("Permission denied", result)
        self.assertTrue(self.work.is_dir())


if __name__ == "__main__":
    unittest.main()
