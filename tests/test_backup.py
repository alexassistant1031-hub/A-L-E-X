"""Tests for timestamped local snapshots (no Git involved)."""

from __future__ import annotations

import sqlite3
import tarfile
import unittest

from controller import BackupManager

from tests.support import TempEnvTestCase


class BackupTests(TempEnvTestCase):
    def setUp(self) -> None:
        super().setUp()
        # A real SQLite database, captured through the SQLite backup API.
        self.db_path = self.project / "data.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("CREATE TABLE items (name TEXT)")
            connection.execute("INSERT INTO items VALUES ('alpha')")
            connection.commit()
        finally:
            connection.close()

        # A file with a .db suffix that is not SQLite: must be copied raw.
        (self.project / "fake.db").write_text("not a database\n", encoding="utf-8")

        # A file symlink and a directory symlink exercise archive counting.
        (self.project / "link_to_core").symlink_to("core.py")
        (self.project / "link_dir").symlink_to("tools")

        self.manager = BackupManager(self.config)

    def test_full_snapshot_is_verified_and_complete(self) -> None:
        record = self.manager.create("unit test snapshot")

        self.assertTrue(record.archive.is_file())
        self.assertTrue(record.checksum_file.is_file())
        self.assertTrue(record.manifest_file.is_file())
        self.assertGreater(record.file_count, 0)
        self.assertGreater(record.total_bytes, 0)

        verification = self.manager.verify(record)
        self.assertTrue(verification.ok, verification.message)
        self.assertEqual(verification.sha256, record.sha256)

        # The archive must contain the core file, the raw fake DB, a valid
        # SQLite snapshot, and the symlinks.
        with tarfile.open(record.archive, "r:gz") as tar:
            names = set(tar.getnames())
            self.assertIn("core.py", names)
            self.assertIn("fake.db", names)
            self.assertIn("data.db", names)
            self.assertIn("link_to_core", names)
            self.assertIn("link_dir", names)
            self.assertIn("staging", names)

            fake = tar.extractfile("fake.db")
            assert fake is not None
            self.assertEqual(fake.read(), b"not a database\n")

            member = tar.getmember("link_to_core")
            self.assertTrue(member.issym())

    def test_sqlite_snapshot_is_consistent(self) -> None:
        record = self.manager.create("sqlite consistency")
        extract_dir = self.base / "extract"
        extract_dir.mkdir()
        with tarfile.open(record.archive, "r:gz") as tar:
            tar.extractall(path=extract_dir, filter="data")

        connection = sqlite3.connect(extract_dir / "data.db")
        try:
            rows = connection.execute("SELECT name FROM items").fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [("alpha",)])

    def test_list_and_prune(self) -> None:
        first = self.manager.create("one")
        second = self.manager.create("two")
        third = self.manager.create("three")

        names = [record.name for record in self.manager.list_backups()]
        self.assertIn(first.name, names)
        self.assertIn(second.name, names)
        self.assertIn(third.name, names)

        removed = self.manager.prune(keep=1)
        self.assertEqual(len(removed), 2)
        remaining = self.manager.list_backups()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].name, third.name)

    def test_restore_into_empty_directory(self) -> None:
        record = self.manager.create("restore source")
        destination = self.base / "restored"
        self.manager.restore(record, destination, confirm="RESTORE")

        self.assertEqual(
            (destination / "core.py").read_text(encoding="utf-8"), "ORIGINAL\n"
        )
        self.assertTrue((destination / "data.db").is_file())
        self.assertTrue((destination / "staging").is_dir())

    def test_restore_requires_confirmation_and_empty_target(self) -> None:
        record = self.manager.create("restore guard")

        with self.assertRaises(Exception):
            self.manager.restore(record, self.base / "nope", confirm="nope")

        target = self.base / "not-empty"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(Exception):
            self.manager.restore(record, target, confirm="RESTORE")
        self.assertTrue((target / "keep.txt").exists())

    def test_path_snapshot_and_restore(self) -> None:
        document = self.work / "doc.txt"
        document.write_text("v1", encoding="utf-8")

        record = self.manager.create_path_snapshot(document, "before edit")
        self.assertIsNotNone(record)
        assert record is not None
        verification = self.manager.verify(record)
        self.assertTrue(verification.ok, verification.message)
        self.assertEqual(
            [item.name for item in self.manager.list_path_snapshots()],
            [record.name],
        )
        self.assertEqual(
            self.manager._record_for(record.name).name, record.name
        )

        document.write_text("v2", encoding="utf-8")
        restored = self.base / "restored-doc"
        self.manager.restore(record, restored, confirm="RESTORE")
        self.assertEqual(
            (restored / "doc.txt").read_text(encoding="utf-8"), "v1"
        )

    def test_missing_target_returns_no_path_snapshot(self) -> None:
        self.assertIsNone(
            self.manager.create_path_snapshot(
                self.work / "does-not-exist.txt", "nothing to save"
            )
        )

    def test_overlarge_path_snapshot_is_refused(self) -> None:
        document = self.work / "big.txt"
        document.write_text("0123456789", encoding="utf-8")
        # Rebuild the config with a tiny per-path limit.
        from dataclasses import replace

        tiny = replace(self.config, max_path_snapshot_bytes=5)
        manager = BackupManager(tiny)
        with self.assertRaises(Exception):
            manager.create_path_snapshot(document, "too big")


if __name__ == "__main__":
    unittest.main()
