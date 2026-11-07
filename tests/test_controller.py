"""Tests for the self-modification controller and the guarded file tool.

Run with:

    .venv/bin/python -m unittest discover -s tests -v

Every test uses temporary directories.  Nothing here touches /var, the real
home directory, or Alex's live database.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from controller import (  # noqa: E402
    ControllerConfig,
    ModificationController,
    ModificationError,
    PathGuardError,
    get_audit,
    reset_audits,
    reset_backup_manager,
    reset_modification_controller,
    set_config,
)
from controller.backup import BackupManager  # noqa: E402
from controller.guard import WritePathGuard  # noqa: E402
from tools.file_tool import FileTool  # noqa: E402
from tools.safety import TypingGuard  # noqa: E402


class ControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="alex-test-"))
        self.project = self.temp / "project"
        self.project.mkdir()
        self.staging = self.project / "staging"
        self.staging.mkdir()
        (self.project / "tools").mkdir()
        (self.project / "memory").mkdir()
        (self.project / "main.py").write_text("print('live')\n", encoding="utf-8")
        (self.project / "tools" / "live.py").write_text("VALUE = 1\n", encoding="utf-8")

        self.work = self.temp / "work"
        self.work.mkdir()
        self.backups = self.temp / "backups"
        self.audit = self.temp / "audit"
        self.trash = self.temp / "trash"

        self.config = ControllerConfig(
            project_root=self.project,
            staging_root=self.staging,
            backup_root=self.backups,
            audit_root=self.audit,
            trash_root=self.trash,
            allowed_write_roots=(self.work,),
            backup_retention=5,
            backup_coalesce_seconds=0,
        )
        set_config(self.config)
        reset_audits()
        reset_backup_manager()
        reset_modification_controller()

    def tearDown(self) -> None:
        reset_audits()
        reset_backup_manager()
        reset_modification_controller()
        # Best-effort cleanup; temporary roots may contain read-only dirs.
        import shutil

        shutil.rmtree(self.temp, ignore_errors=True)


class GuardTests(ControllerTestCase):
    def test_staging_and_work_are_writable(self) -> None:
        guard = WritePathGuard(self.config)
        self.assertEqual(guard.require_writable(self.staging / "a.txt"), self.staging / "a.txt")
        self.assertEqual(guard.require_writable(self.work / "b.txt"), self.work / "b.txt")

    def test_core_and_storage_are_denied(self) -> None:
        guard = WritePathGuard(self.config)
        for path in (
            self.project / "main.py",
            self.project / "tools" / "live.py",
            self.backups / "x.tar.gz",
            self.audit / "file.jsonl",
            self.trash / "old",
        ):
            with self.assertRaises(PathGuardError):
                guard.require_writable(path)

    def test_credentials_are_denied(self) -> None:
        guard = WritePathGuard(self.config)
        for path in (
            self.work / ".env",
            self.work / ".ssh" / "id_rsa",
            self.work / "server.pem",
        ):
            with self.assertRaises(PathGuardError):
                guard.require_writable(path)

    def test_symlink_escape_is_denied(self) -> None:
        guard = WritePathGuard(self.config)
        link = self.staging / "escape.txt"
        link.symlink_to(self.project / "main.py")
        with self.assertRaises(PathGuardError):
            guard.require_writable(link)

    def test_allowed_roots_cannot_be_deleted(self) -> None:
        guard = WritePathGuard(self.config)
        for path in (self.staging, self.work):
            with self.assertRaises(PathGuardError):
                guard.require_deletable(path)


class BackupTests(ControllerTestCase):
    def test_create_verify_and_list(self) -> None:
        manager = BackupManager(self.config)
        record = manager.create("unit test")
        self.assertTrue(record.archive.is_file())
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertTrue(manager.verify(record).ok)
        self.assertEqual(len(manager.list_backups()), 1)

    def test_tampered_archive_fails_verification(self) -> None:
        manager = BackupManager(self.config)
        record = manager.create("tamper test")
        with open(record.archive, "ab") as handle:
            handle.write(b"corruption")
        self.assertFalse(manager.verify(record).ok)

    def test_retention_prunes_old_snapshots(self) -> None:
        manager = BackupManager(self.config)
        for index in range(4):
            manager.create(f"snapshot {index}")
        self.assertEqual(len(manager.list_backups()), 4)

        manager.prune(keep=2)
        self.assertEqual(len(manager.list_backups()), 2)

    def test_restore_requires_confirmation_and_empty_destination(self) -> None:
        manager = BackupManager(self.config)
        record = manager.create("restore test")
        destination = self.temp / "restored"
        with self.assertRaises(Exception):
            manager.restore(record, destination, confirm="yes")
        manager.restore(record, destination, confirm="RESTORE")
        self.assertTrue((destination / "main.py").is_file())

    def test_path_snapshot_round_trip(self) -> None:
        manager = BackupManager(self.config)
        target = self.work / "doc.txt"
        target.write_text("version one\n", encoding="utf-8")
        first = manager.create_path_snapshot(target, "before edit")
        self.assertIsNotNone(first)
        target.write_text("version two\n", encoding="utf-8")

        destination = self.temp / "recovered"
        manager.restore(first, destination, confirm="RESTORE")
        self.assertEqual((destination / "doc.txt").read_text(encoding="utf-8"), "version one\n")


class FileToolTests(ControllerTestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_write_staging_and_deny_core(self) -> None:
        tool = FileTool(config=self.config)
        result = self.run_async(tool.execute("write", "staging/notes.txt", "hello"))
        self.assertIn("Written", result)
        self.assertEqual((self.staging / "notes.txt").read_text(encoding="utf-8"), "hello")

        before = len(BackupManager(self.config).list_backups())
        denied = self.run_async(tool.execute("write", "main.py", "hacked"))
        self.assertIn("Permission denied", denied)
        self.assertEqual((self.project / "main.py").read_text(encoding="utf-8"), "print('live')\n")
        # A denied write must not create a pointless snapshot.
        self.assertEqual(len(BackupManager(self.config).list_backups()), before)

    def test_append_copy_move_and_delete(self) -> None:
        tool = FileTool(config=self.config)
        self.run_async(tool.execute("write", "staging/notes.txt", "hello"))
        self.assertIn("Appended", self.run_async(tool.execute("append", "staging/notes.txt", " world")))

        copied = self.run_async(
            tool.execute("copy", "main.py", destination=str(self.work / "copy.py"))
        )
        self.assertIn("Copied", copied)
        self.assertTrue((self.work / "copy.py").is_file())

        refused = self.run_async(
            tool.execute("move", "main.py", destination=str(self.work / "moved.py"))
        )
        self.assertIn("Permission denied", refused)
        self.assertTrue((self.project / "main.py").is_file())

        deleted = self.run_async(tool.execute("delete", "staging/notes.txt"))
        self.assertIn("Moved file to trash", deleted)
        self.assertFalse((self.staging / "notes.txt").exists())
        self.assertEqual(len(list(self.trash.iterdir())), 1)

    def test_mkdir_and_read(self) -> None:
        tool = FileTool(config=self.config)
        result = self.run_async(tool.execute("mkdir", str(self.work / "sub")))
        self.assertIn("Created directory", result)
        self.assertTrue((self.work / "sub").is_dir())

        content = self.run_async(tool.execute("read", "main.py"))
        self.assertIn("print('live')", content)

    def test_write_to_working_directory_is_snapshotted(self) -> None:
        tool = FileTool(config=self.config)
        target = self.work / "doc.txt"
        self.run_async(tool.execute("write", str(target), "one"))
        self.run_async(tool.execute("write", str(target), "two"))
        snapshots = BackupManager(self.config).list_path_snapshots()
        self.assertEqual(len(snapshots), 1)


class AuditTests(ControllerTestCase):
    def test_hash_chain_and_tamper_detection(self) -> None:
        log = get_audit("file", self.config)
        log.write("first", path="a")
        log.write("second", path="b")
        ok, count, _ = log.verify()
        self.assertTrue(ok)
        self.assertEqual(count, 2)

        path = self.audit / "file.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["event"] = "changed"
        lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, _, message = log.verify()
        self.assertFalse(ok)
        self.assertIn("hash mismatch", message)

    def test_typing_guard_uses_secure_log(self) -> None:
        guard = TypingGuard(config=self.config)
        guard.record("hello", "normal", "", "ok")
        log = get_audit("typing", self.config)
        ok, count, _ = log.verify()
        self.assertTrue(ok)
        self.assertEqual(count, 1)
        self.assertTrue((self.audit / "typing.jsonl").is_file())


class PromotionTests(ControllerTestCase):
    def test_apply_staged_requires_confirmation(self) -> None:
        controller = ModificationController(self.config)
        staged = self.staging / "tools" / "live.py"
        staged.parent.mkdir()
        staged.write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaises(ModificationError):
            controller.apply_staged(staged, self.project / "tools" / "live.py")

        controller.apply_staged(
            staged, self.project / "tools" / "live.py", confirm=True
        )
        self.assertEqual(
            (self.project / "tools" / "live.py").read_text(encoding="utf-8"),
            "VALUE = 2\n",
        )

    def test_apply_staged_rejects_outside_project(self) -> None:
        controller = ModificationController(self.config)
        staged = self.staging / "outside.txt"
        staged.write_text("x", encoding="utf-8")
        with self.assertRaises(ModificationError):
            controller.apply_staged(staged, self.work / "outside.txt", confirm=True)


class ConfigTests(ControllerTestCase):
    def test_staging_must_be_inside_project(self) -> None:
        with self.assertRaises(ValueError):
            ControllerConfig(
                project_root=self.project,
                staging_root=self.temp / "elsewhere",
                backup_root=self.backups,
                audit_root=self.audit,
                trash_root=self.trash,
                allowed_write_roots=(self.work,),
            )

    def test_backup_must_be_outside_project(self) -> None:
        with self.assertRaises(ValueError):
            ControllerConfig(
                project_root=self.project,
                staging_root=self.staging,
                backup_root=self.project / "backups",
                audit_root=self.audit,
                trash_root=self.trash,
                allowed_write_roots=(self.work,),
            )

    def test_allowed_root_cannot_be_project_or_home(self) -> None:
        for root in (self.project, Path.home()):
            with self.assertRaises(ValueError):
                ControllerConfig(
                    project_root=self.project,
                    staging_root=self.staging,
                    backup_root=self.backups,
                    audit_root=self.audit,
                    trash_root=self.trash,
                    allowed_write_roots=(root,),
                )


if __name__ == "__main__":
    unittest.main()
