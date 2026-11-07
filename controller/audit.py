"""Tamper-evident audit logging owned by the controller.

Logs are append-only JSON Lines files stored in ``/var/log/alex-controller``
(or ``ALEX_AUDIT_ROOT``), mode 0600 inside a 0700 directory.  Every record is
hash-chained to the previous one, so editing or deleting a line is detectable
with :meth:`SecureAuditLog.verify`.

The chain is tamper-evident, not tamper-proof: an attacker who can rewrite the
whole file and every anchor can forge a new chain.  Keeping the directory
owned by the controller and unreadable to other users is what makes that
attack impractical.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ControllerConfig, get_config
from .errors import AuditError
from .storage import ensure_private_dir, secure_open_append

_GENESIS = "0" * 64
_MAX_DEPTH = 5
_MAX_ITEMS = 50


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _record_hash(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _sanitize(value: Any, limit: int, depth: int = 0) -> Any:
    """Convert arbitrary values into bounded, JSON-safe audit fields."""
    if depth > _MAX_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\x00", "\\0")
        return text if len(text) <= limit else text[: limit - 1] + "…"
    if isinstance(value, Path):
        return _sanitize(str(value), limit, depth + 1)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["<truncated>"] = True
                break
            out[str(key)[:128]] = _sanitize(item, limit, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        out_list = []
        for index, item in enumerate(value):
            if index >= _MAX_ITEMS:
                out_list.append("<truncated>")
                break
            out_list.append(_sanitize(item, limit, depth + 1))
        return out_list
    return _sanitize(repr(value), limit, depth + 1)


class SecureAuditLog:
    """Append-only, hash-chained JSONL log with cross-process locking."""

    def __init__(
        self,
        path: Path,
        *,
        max_field_chars: int = 2000,
        fsync: bool = True,
    ) -> None:
        self.path = Path(path)
        self._max_field_chars = max_field_chars
        self._fsync = fsync
        self._thread_lock = threading.Lock()
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        ensure_private_dir(self.path.parent)

    # ── writing ───────────────────────────────────────────────────────────────

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event.  Raises :class:`AuditError` on failure."""
        event = str(event).strip()
        if not event:
            raise AuditError("audit event name must not be empty")

        record: dict[str, Any] = {
            "ts": _utc_now(),
            "event": _sanitize(event, 128),
            "pid": os.getpid(),
        }
        for key, value in fields.items():
            record[str(key)[:128]] = _sanitize(
                value, self._max_field_chars
            )

        with self._thread_lock:
            ensure_private_dir(self.path.parent)
            try:
                lock_fd = os.open(
                    self._lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError as exc:
                raise AuditError(f"cannot open audit lock {self._lock_path}: {exc}") from exc

            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                previous = self._read_last_locked()
                record["seq"] = int(previous["seq"]) + 1 if previous else 1
                record["prev"] = previous["hash"] if previous else _GENESIS
                record["hash"] = _record_hash(record)
                line = _canonical(record) + b"\n"

                fd = secure_open_append(self.path)
                try:
                    view = memoryview(line)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise AuditError("short write to audit log")
                        view = view[written:]
                    if self._fsync:
                        os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise AuditError(f"cannot append to audit log {self.path}: {exc}") from exc
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
        return record

    def _open_read_fd(self) -> int | None:
        """Open the log for reading without following a symlink.

        Returns ``None`` when the log does not exist yet.  Raises
        :class:`AuditError` for a symlink, a special file, or a permissions
        problem, so a swapped symlink cannot redirect verification.
        """
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AuditError(f"cannot read audit log {self.path}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise AuditError(
                    f"audit log is not a regular file: {self.path}"
                )
        except Exception:
            os.close(fd)
            raise
        return fd

    def _read_last_locked(self) -> dict[str, Any] | None:
        """Return the last valid record, or None for an empty log."""
        fd = self._open_read_fd()
        if fd is None:
            return None
        with os.fdopen(fd, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return None
            position = size
            buffer = b""
            while position > 0:
                step = min(4096, position)
                position -= step
                fh.seek(position)
                buffer = fh.read(step) + buffer
                lines = buffer.splitlines()
                if position > 0 and lines:
                    lines = lines[1:]  # first line may be partial
                for raw in reversed(lines):
                    if raw.strip():
                        try:
                            record = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise AuditError(
                                f"audit log {self.path} ends with invalid JSON; "
                                "refusing to append to a broken chain"
                            ) from exc
                        if not isinstance(record, dict) or "hash" not in record:
                            raise AuditError(
                                f"audit log {self.path} has an incomplete tail"
                            )
                        return record
        return None

    # ── reading and verification ──────────────────────────────────────────────

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Yield every record in order.  Raises on malformed JSON."""
        fd = self._open_read_fd()
        if fd is None:
            return
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise AuditError(
                            f"{self.path}:{number} is not valid JSON: {exc}"
                        ) from exc
                    yield record
        except UnicodeDecodeError as exc:
            raise AuditError(f"{self.path} is not valid UTF-8: {exc}") from exc

    def verify(self) -> tuple[bool, int, str]:
        """Verify the hash chain.

        Returns ``(ok, count, message)``.  ``ok`` is False on the first broken
        sequence number, previous hash, record hash, or malformed line.
        """
        previous = _GENESIS
        expected_seq = 1
        count = 0
        try:
            events = self.iter_events()
            if events is None:
                return True, 0, "empty"
            for record in events:
                if record.get("seq") != expected_seq:
                    return False, count, f"sequence mismatch at record {expected_seq}"
                if record.get("prev") != previous:
                    return False, count, f"previous-hash mismatch at record {expected_seq}"
                if record.get("hash") != _record_hash(record):
                    return False, count, f"hash mismatch at record {expected_seq}"
                previous = str(record["hash"])
                expected_seq += 1
                count += 1
        except AuditError as exc:
            return False, count, str(exc)
        return True, count, "ok"

    def tail(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the last ``limit`` records."""
        if limit <= 0:
            return []
        try:
            records = list(self.iter_events())
        except AuditError:
            raise
        return records[-limit:]

    def size_bytes(self) -> int:
        try:
            st = self.path.lstat()
        except FileNotFoundError:
            return 0
        if stat.S_ISLNK(st.st_mode):
            raise AuditError(f"audit log is a symlink: {self.path}")
        if not stat.S_ISREG(st.st_mode):
            raise AuditError(f"audit log is not a regular file: {self.path}")
        return st.st_size


_AUDITS: dict[Path, SecureAuditLog] = {}
_AUDIT_LOCK = threading.Lock()


def get_audit(
    stream: str = "controller",
    config: ControllerConfig | None = None,
) -> SecureAuditLog:
    """Return the shared log for a named stream (``file``, ``typing``, ...)."""
    stream = stream.strip().lower()
    if not stream or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in stream
    ):
        raise AuditError(f"invalid audit stream name: {stream!r}")

    cfg = config or get_config()
    path = (cfg.audit_root / f"{stream}.jsonl").resolve(strict=False)
    with _AUDIT_LOCK:
        log = _AUDITS.get(path)
        if log is None:
            log = SecureAuditLog(
                path,
                max_field_chars=cfg.audit_max_field_chars,
            )
            _AUDITS[path] = log
        return log


def reset_audits() -> None:
    """Forget cached log handles (used by tests)."""
    with _AUDIT_LOCK:
        _AUDITS.clear()
