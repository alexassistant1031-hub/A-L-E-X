"""Tests for the deny-by-default write-path guard."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from controller import PathGuardError, WritePathGuard

from tests.support import TempEnvTestCase


class GuardTests(TempEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.guard = WritePathGuard(self.config)

    def test_staging_and_working_roots_are_writable(self) -> None:
        staging_file = self.staging / "draft.txt"
        work_file = self.work / "notes" / "todo.txt"
        self.assertEqual(self.guard.require_writable(staging_file), staging_file)
        self.assertEqual(self.guard.require_writable(work_file), work_file)
        self.assertIn(str(self.staging), self.guard.describe_allowed())
        self.assertIn(str(self.work), self.guard.describe_allowed())

    def test_project_core_is_denied(self) -> None:
        for target in (
            self.project / "core.py",
            self.project / "tools" / "helper.py",
            self.project / "memory" / "new.log",
            self.project / "controller" / "config.py",
        ):
            with self.subTest(target=target):
                with self.assertRaises(PathGuardError) as ctx:
                    self.guard.require_writable(target)
                self.assertIn("protected project core", str(ctx.exception))

    def test_outside_roots_are_denied(self) -> None:
        outside = self.base / "elsewhere" / "file.txt"
        with self.assertRaises(PathGuardError):
            self.guard.require_writable(outside)
        with self.assertRaises(PathGuardError):
            self.guard.require_writable(Path("/etc/passwd"))

    def test_credentials_are_read_only_even_in_working_root(self) -> None:
        for name in (".env", ".env.local", "id_rsa", "private.pem", "credentials.json"):
            with self.subTest(name=name):
                with self.assertRaises(PathGuardError):
                    self.guard.require_writable(self.work / name)

        ssh = self.work / ".ssh" / "config"
        with self.assertRaises(PathGuardError):
            self.guard.require_writable(ssh)

    def test_symlink_escape_from_staging_is_denied(self) -> None:
        link = self.staging / "escape.py"
        link.symlink_to(self.project / "core.py")
        with self.assertRaises(PathGuardError):
            self.guard.require_writable(link)

    def test_delete_refuses_roots_but_allows_staged_files(self) -> None:
        draft = self.staging / "draft.txt"
        draft.write_text("draft", encoding="utf-8")

        self.assertEqual(self.guard.require_deletable(draft), draft)
        for root in (
            self.project,
            self.staging,
            self.work,
            self.backup_root,
            self.audit_root,
            self.config.trash_root,
        ):
            with self.subTest(root=root):
                with self.assertRaises(PathGuardError):
                    self.guard.require_deletable(root)

    def test_open_for_write_validates_and_writes(self) -> None:
        target = self.staging / "nested" / "file.txt"
        with self.guard.open_for_write(target) as fh:
            fh.write("hello")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")

        with self.guard.open_for_write(target, append=True) as fh:
            fh.write(" world")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world")

        with self.assertRaises(PathGuardError):
            with self.guard.open_for_write(self.project / "core.py") as fh:
                fh.write("tampered")

    def test_open_for_write_refuses_symlink_to_protected_file(self) -> None:
        link = self.staging / "link.txt"
        link.symlink_to(self.project / "core.py")
        with self.assertRaises(PathGuardError):
            with self.guard.open_for_write(link) as fh:
                fh.write("nope")
        self.assertEqual(
            (self.project / "core.py").read_text(encoding="utf-8"), "ORIGINAL\n"
        )

    def test_open_for_write_refuses_hardlinked_file(self) -> None:
        target = self.staging / "linked.txt"
        target.write_text("original", encoding="utf-8")
        os.link(target, self.staging / "other-name.txt")

        with self.assertRaises(PathGuardError):
            with self.guard.open_for_write(target) as fh:
                fh.write("tampered")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_open_for_write_refuses_special_files(self) -> None:
        fifo = self.staging / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(PathGuardError):
            with self.guard.open_for_write(fifo) as fh:
                fh.write("nope")

    def test_require_directory_creates_only_inside_policy(self) -> None:
        created = self.guard.require_directory(
            self.staging / "deep" / "nested", create=True
        )
        self.assertTrue(created.is_dir())

        with self.assertRaises(PathGuardError):
            self.guard.require_directory(
                self.project / "tools" / "newdir", create=True
            )

    def test_explain_does_not_raise(self) -> None:
        allowed, reason = self.guard.explain(self.staging / "x.txt")
        self.assertTrue(allowed)
        self.assertIn("staging", reason)
        allowed, reason = self.guard.explain(self.project / "core.py")
        self.assertFalse(allowed)
        self.assertIn("protected", reason)


if __name__ == "__main__":
    unittest.main()
