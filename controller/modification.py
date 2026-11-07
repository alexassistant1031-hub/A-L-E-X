"""Backup-before-modification pipeline for Alex's filesystem tool.

This is the Phase 1 orchestrator.  Every mutating operation goes through the
same sequence, and any failure aborts before the target is touched:

    1. policy check      WritePathGuard (staging + user working directories)
    2. safety snapshot   full project backup and/or single-path snapshot
    3. audit intent      append to the controller's hash-chained log
    4. mutate            atomic write / trash-move / copy / move
    5. audit outcome     success or failure, with the snapshot name

Nothing here uses Git.  The controller never runs ``git`` and never depends on
a repository being present; snapshots are self-contained ``tar.gz`` archives
with SHA-256 checksums and JSON manifests.

The module is deliberately import-safe: it creates no directories and starts
no threads until the first modification is attempted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .audit import SecureAuditLog, get_audit
from .backup import BackupManager, BackupRecord
from .config import ControllerConfig, get_config
from .errors import AuditError, BackupError, ModificationError
from .guard import WritePathGuard
from .storage import ensure_private_dir

logger = logging.getLogger("alex.controller.modification")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so a rename survives a crash."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@dataclass(frozen=True)
class SafetySnapshot:
    """What was saved before a modification."""

    project: BackupRecord | None = None
    paths: tuple[BackupRecord, ...] = ()
    reused: bool = False

    @property
    def names(self) -> tuple[str, ...]:
        items = []
        if self.project:
            items.append(self.project.name)
        items.extend(record.name for record in self.paths)
        return tuple(items)

    def describe(self) -> str:
        if not self.project and not self.paths:
            return "no existing state to snapshot"
        parts = []
        if self.project:
            parts.append(
                f"project backup {self.project.name}"
                + (" (reused)" if self.reused else "")
            )
        for record in self.paths:
            parts.append(f"snapshot {record.name}")
        return ", ".join(parts)


class ModificationController:
    """Apply guarded filesystem changes with snapshots and auditing."""

    def __init__(
        self,
        config: ControllerConfig | None = None,
        *,
        backup: BackupManager | None = None,
        audit: SecureAuditLog | None = None,
        guard: WritePathGuard | None = None,
    ) -> None:
        self.config = config or get_config()
        self.guard = guard or WritePathGuard(self.config)
        self.backup = backup or BackupManager(self.config, audit=audit)
        self._audit = audit
        self._lock = threading.Lock()
        self._last_project_backup: tuple[float, BackupRecord] | None = None

    # ── audit ─────────────────────────────────────────────────────────────────

    @property
    def audit(self) -> SecureAuditLog:
        if self._audit is None:
            self._audit = get_audit("file", self.config)
        return self._audit

    def record(self, event: str, **fields: Any) -> None:
        """Append to the controller-owned audit log.

        Callers must treat :class:`AuditError` as fatal for a modification:
        if the trail cannot be written, the change is not applied.
        """
        self.audit.write(event, **fields)

    # ── readiness ─────────────────────────────────────────────────────────────

    def check_ready(self) -> tuple[bool, str]:
        """Verify that backup and audit storage are usable.

        Returns ``(ready, message)``.  This creates the private directories if
        needed, so call it during startup or immediately before a write.
        """
        try:
            ensure_private_dir(self.config.backup_root)
            self.backup.list_backups()
        except Exception as exc:
            return False, f"backup storage unavailable: {exc}"
        try:
            ensure_private_dir(self.config.audit_root)
            self.audit.size_bytes()
        except Exception as exc:
            return False, f"audit storage unavailable: {exc}"
        return True, "ready"

    def status(self) -> dict[str, Any]:
        ready, message = self.check_ready()
        return {
            "ready": ready,
            "message": message,
            "project_root": str(self.config.project_root),
            "staging_root": str(self.config.staging_root),
            "backup_root": str(self.config.backup_root),
            "audit_root": str(self.config.audit_root),
            "trash_root": str(self.config.trash_root),
            "backup_fallback": self.config.backup_root_is_fallback,
            "audit_fallback": self.config.audit_root_is_fallback,
            "allowed_write_roots": [str(p) for p in self.config.allowed_write_roots],
        }

    # ── snapshots ─────────────────────────────────────────────────────────────

    def ensure_snapshot(
        self,
        action: str,
        paths: Iterable[str | Path],
        *,
        reason: str = "",
        snapshot_external: bool = True,
        coalesce: bool = True,
    ) -> SafetySnapshot:
        """Create every snapshot needed before ``action`` touches ``paths``.

        Project paths trigger one full project snapshot (coalesced over a short
        window unless ``coalesce=False``).  Paths outside the project trigger a
        snapshot of that file or directory.  Raises :class:`BackupError` if a
        required snapshot fails.

        Classification uses the *entry* path (the parent is resolved, the
        final component is not) so deleting or moving a symlink inside the
        project still triggers a project snapshot instead of being mistaken
        for its external target.
        """
        project = self.config.project_root
        in_project: list[Path] = []
        outside: list[Path] = []
        for item in paths:
            raw = Path(item).expanduser()
            if not raw.is_absolute():
                raw = Path.cwd() / raw
            try:
                entry = raw.parent.resolve(strict=False) / raw.name
                resolved = raw.resolve(strict=False)
            except OSError:
                # A path we cannot resolve is treated as project-affecting so
                # the caller takes the conservative full snapshot.
                in_project.append(raw)
                continue
            if _within(entry, project) or _within(resolved, project):
                in_project.append(entry)
            else:
                outside.append(resolved)

        project_record: BackupRecord | None = None
        reused = False
        if in_project:
            project_record, reused = self._project_snapshot(
                reason or f"{action} {in_project[0].name}",
                coalesce=coalesce,
            )

        path_records: list[BackupRecord] = []
        if snapshot_external:
            seen: set[Path] = set()
            for target in outside:
                if target in seen:
                    continue
                seen.add(target)
                if not target.exists():
                    continue
                record = self.backup.create_path_snapshot(
                    target, reason or f"{action} {target.name}"
                )
                if record is not None:
                    path_records.append(record)

        return SafetySnapshot(
            project=project_record,
            paths=tuple(path_records),
            reused=reused,
        )

    def _project_snapshot(
        self, reason: str, *, coalesce: bool = True
    ) -> tuple[BackupRecord, bool]:
        """Return a recent project snapshot or create a new one."""
        window = max(0, int(self.config.backup_coalesce_seconds))
        now = time.monotonic()
        with self._lock:
            if coalesce and window and self._last_project_backup is not None:
                created, record = self._last_project_backup
                if now - created <= window and record.archive.is_file():
                    return record, True

            record = self.backup.create(reason, trigger="automatic")
            self._last_project_backup = (time.monotonic(), record)
            return record, False

    # ── mutating operations ───────────────────────────────────────────────────

    def write_text(
        self, path: str | Path, content: str, *, append: bool = False
    ) -> Path:
        """Write UTF-8 text atomically (or append) after a policy check."""
        target = self.guard.require_writable(path)
        data = content.encode("utf-8")
        if len(data) > self.config.max_write_bytes:
            raise ModificationError(
                f"refusing to write {len(data)} bytes; limit is "
                f"{self.config.max_write_bytes} (ALEX_MAX_WRITE_BYTES)"
            )
        if append:
            self.guard.require_directory(target.parent, create=True)
            with self.guard.open_for_write(target, append=True) as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            return target

        self.guard.require_directory(target.parent, create=True)
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600

        fd, temp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".alex-tmp"
        )
        temp = Path(temp_name)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            # Re-validate immediately before publishing the new content.
            self.guard.require_writable(target)
            os.replace(temp, target)
            _fsync_dir(target.parent)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return target

    def apply_staged(
        self,
        staged: str | Path,
        target: str | Path,
        *,
        confirm: bool = False,
    ) -> Path:
        """Promote a file from ``staging/`` into the live project tree.

        This is the privileged Phase 2 hook.  It is **not** exposed to the
        model: callers must pass ``confirm=True`` explicitly, the source must
        live inside ``staging/``, and the destination must be inside the
        project but outside staging, the backup root and the audit root.

        A full project snapshot is created before the live file is replaced,
        so every promotion has a one-command rollback point.  The replacement
        is atomic (temp file + fsync + rename).
        """
        if confirm is not True:
            raise ModificationError(
                "apply_staged requires confirm=True; this is an operator action"
            )

        staged_path = self.guard.resolve(staged)
        if not _within(staged_path, self.config.staging_root):
            raise ModificationError(f"staged file is outside staging/: {staged_path}")
        if staged_path.is_symlink() or not staged_path.is_file():
            raise ModificationError(f"staged path is not a regular file: {staged_path}")

        target_path = self.guard.resolve(target)
        if target_path.is_symlink():
            raise ModificationError(f"refusing to replace a symlink: {target_path}")
        if _within(target_path, self.config.staging_root):
            raise ModificationError("target is already inside staging/")
        if not _within(target_path, self.config.project_root):
            raise ModificationError(f"target is outside the project: {target_path}")
        for label, root in (
            ("backup storage", self.config.backup_root),
            ("audit storage", self.config.audit_root),
            ("trash", self.config.trash_root),
        ):
            if _within(target_path, root):
                raise ModificationError(f"target is inside {label}: {target_path}")
        if self.guard.is_sensitive(target_path):
            raise ModificationError(f"target is credential material: {target_path}")

        data = staged_path.read_bytes()
        if len(data) > self.config.max_write_bytes:
            raise ModificationError(
                f"staged file is {len(data)} bytes; limit is "
                f"{self.config.max_write_bytes}"
            )

        snapshot = self.ensure_snapshot(
            "apply_staged",
            [target_path],
            reason=f"promote {target_path.name}",
            snapshot_external=False,
            coalesce=False,
        )
        self.record(
            "staged_promotion_intent",
            staged=str(staged_path),
            target=str(target_path),
            snapshot=list(snapshot.names),
            sha256_of_staged=_sha256_bytes(data),
        )

        try:
            mode = target_path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        try:
            self._atomic_write_bytes(target_path, data, mode=mode)
        except Exception as exc:
            try:
                self.record(
                    "staged_promotion_failed",
                    staged=str(staged_path),
                    target=str(target_path),
                    error=f"{exc.__class__.__name__}: {exc}",
                )
            except AuditError:
                pass
            raise

        self.record(
            "staged_promotion_ok",
            staged=str(staged_path),
            target=str(target_path),
            bytes=len(data),
            snapshot=list(snapshot.names),
        )
        return target_path

    def _atomic_write_bytes(self, target: Path, data: bytes, *, mode: int) -> None:
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=parent, prefix=f".{target.name}.", suffix=".alex-tmp"
        )
        temp = Path(temp_name)
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise ModificationError(f"short write to {temp}")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp, target)
            _fsync_dir(parent)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def mkdir(self, path: str | Path) -> Path:
        """Create a directory after a policy check."""
        return self.guard.require_directory(path, create=True)

    def delete(self, path: str | Path) -> tuple[Path, Path]:
        """Move a file/directory to the controller trash.  Returns (src, dest)."""
        target = self.guard.require_deletable(path)
        if not target.exists() and not target.is_symlink():
            raise FileNotFoundError(f"path not found: {target}")

        trash = self.config.trash_root
        ensure_private_dir(trash)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        destination = trash / f"{target.name}.{stamp}.{suffix}"

        shutil.move(str(target), str(destination))
        return target, destination

    def copy(self, source: str | Path, destination: str | Path) -> Path:
        """Copy a file or directory after checking both ends."""
        src = self.guard.require_safe_source(source)
        dst = self.guard.require_writable(destination)
        if src.is_dir() and (dst == src or _within(dst, src)):
            raise ModificationError("cannot copy a directory into itself")

        if src.is_dir():
            self.guard.require_directory(dst.parent, create=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            self.guard.require_directory(dst.parent, create=True)
            shutil.copy2(src, dst)
        return dst

    def move(self, source: str | Path, destination: str | Path) -> Path:
        """Move a file or directory after checking both ends."""
        src = self.guard.require_deletable(source)
        dst = self.guard.require_writable(destination)
        if src.is_dir() and (dst == src or _within(dst, src)):
            raise ModificationError("cannot move a directory into itself")

        self.guard.require_directory(dst.parent, create=True)
        shutil.move(str(src), str(dst))
        return dst

    def restore_path_snapshot(
        self, record: BackupRecord | str | Path, destination: str | Path
    ) -> Path:
        """Extract a single-path snapshot into ``destination``.

        Requires the destination to be empty (or absent) so a recovery never
        silently overwrites current work.  This is an operator action, not
        something the voice agent can call.
        """
        return self.backup.restore(
            record, destination, confirm="RESTORE", overwrite=False
        )


_MANAGER: ModificationController | None = None
_MANAGER_LOCK = threading.Lock()


def get_modification_controller() -> ModificationController:
    """Return the process-wide modification controller."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = ModificationController(get_config())
        return _MANAGER


def reset_modification_controller() -> None:
    """Forget the singleton (used by tests and after a config change)."""
    global _MANAGER
    with _MANAGER_LOCK:
        _MANAGER = None
