"""Tests for the controller-owned, hash-chained audit log."""

from __future__ import annotations

import json
import unittest

from controller import AuditError, SecureAuditLog

from tests.support import TempEnvTestCase


class AuditTests(TempEnvTestCase):
    def _log(self, name: str = "test.jsonl") -> SecureAuditLog:
        return SecureAuditLog(self.audit_root / name, fsync=False)

    def test_hash_chain_and_tail(self) -> None:
        log = self._log()
        first = log.write("file_change_requested", action="write", path="/a")
        second = log.write("file_change_completed", action="write", path="/a")

        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["prev"], first["hash"])

        ok, count, message = log.verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 2)

        tail = log.tail(1)
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0]["seq"], 2)

    def test_tampering_is_detected(self) -> None:
        log = self._log()
        log.write("one", value=1)
        log.write("two", value=2)

        path = log.path
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["event"] = "tampered"
        lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, count, message = log.verify()
        self.assertFalse(ok)
        self.assertEqual(count, 0)
        self.assertIn("hash mismatch", message)

    def test_deleted_line_is_detected(self) -> None:
        log = self._log()
        log.write("one")
        log.write("two")
        log.write("three")

        lines = log.path.read_text(encoding="utf-8").splitlines()
        log.path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

        ok, _count, message = log.verify()
        self.assertFalse(ok)
        self.assertIn("sequence mismatch", message)

    def test_symlinked_log_is_refused(self) -> None:
        self.audit_root.mkdir(parents=True, exist_ok=True)
        outside = self.base / "outside.jsonl"
        outside.write_text("", encoding="utf-8")
        link = self.audit_root / "link.jsonl"
        link.symlink_to(outside)

        log = SecureAuditLog(link, fsync=False)
        with self.assertRaises(AuditError):
            log.write("should_not_append")
        with self.assertRaises(AuditError):
            list(log.iter_events())
        with self.assertRaises(AuditError):
            log.size_bytes()
        self.assertEqual(outside.read_text(encoding="utf-8"), "")

    def test_invalid_tail_refuses_append(self) -> None:
        log = self._log()
        log.write("one")
        with open(log.path, "ab") as fh:
            fh.write(b'{"broken":')
        with self.assertRaises(AuditError):
            log.write("two")

    def test_invalid_utf8_is_detected(self) -> None:
        log = self._log()
        log.write("one")
        with open(log.path, "ab") as fh:
            fh.write(b"\xff\xfe\n")

        ok, _count, message = log.verify()
        self.assertFalse(ok)
        self.assertIn("UTF-8", message)
        with self.assertRaises(AuditError):
            log.write("two")

    def test_multiple_records_keep_chain_valid(self) -> None:
        log = self._log("many.jsonl")
        for index in range(50):
            log.write("event", index=index)
        ok, count, message = log.verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 50)


if __name__ == "__main__":
    unittest.main()
