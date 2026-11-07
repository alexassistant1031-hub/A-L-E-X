"""Full local snapshots of the project, stored outside the project tree.

A snapshot is a ``tar.gz`` archive plus a SHA-256 file and a JSON manifest:

    /var/backups/alex/
        alex-20261028T173116123456Z-pre-modification-ab12cd34.tar.gz
        ...tar.gz.sha256
        ...tar.gz.manifest.json

The archive is written to a temporary name, verified, and only then renamed.
SQLite databases are captured through the SQLite backup API so the snapshot
contains a consistent database even while Alex is running.  No Git repository
or worktree is used anywhere in this module.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .audit import SecureAuditLog, get_audit
from .config import ControllerConfig, get_config
from .errors import AuditError, BackupError
from .storage import ensure_private_dir, fsync_file, atomic_write_text

_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_SQLITE_MAGIC = b"SQLite format 3\x00"
_BACKUP_GLOB = "alex-*.tar.gz"
_PATH_BACKUP_DIR = "paths"
_PATH_BACKUP_GLOB = "alex-path-*.tar.gz"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_reason(reason: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(reason)).strip()
    return text[:120] or "unspecified"


def _slug(reason: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in reason)
    return "-".join(part for part in cleaned.split("-") if part)[:40] or "snapshot"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_tarinfo(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Refuse sockets/FIFOs/devices; keep regular files, dirs and symlinks."""
    if info.isdev() or info.isfifo():
        return None
    return info


@dataclass(frozen=True)
class BackupRecord:
    name: str
    archive: Path
    checksum_file: Path
    manifest_file: Path
    sha256: str
    created_at: str
    reason: str
    trigger: str
    file_count: int
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": 1,
            "name": self.name,
            "archive": str(self.archive),
            "checksum_file": str(self.checksum_file),
            "manifest_file": str(self.manifest_file),
            "sha256": self.sha256,
            "created_at": self.created_at,
            "reason": self.reason,
            "trigger": self.trigger,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True)
class BackupVerification:
    ok: bool
    sha256: str
    file_count: int
    message: str


class BackupManager:
    """Create, verify, list, prune, and restore local project snapshots."""

    def __init__(
        self,
        config: ControllerConfig | None = None,
        *,
        audit: SecureAuditLog | None = None,
    ) -> None:
        self.config = config or get_config()
        self.root = self.config.backup_root
        self._audit = audit
        self._thread_lock = threading.Lock()

    # ── creation ──────────────────────────────────────────────────────────────

    def create(self, reason: str, *, trigger: str = "manual") -> BackupRecord:
        """Create and verify a full snapshot.  Raises :class:`BackupError`."""
        reason = _clean_reason(reason)
        ensure_private_dir(self.root)
        with self._exclusive_lock():
            self._check_free_space()
            stamp = _utc_stamp()
            name = f"alex-{stamp}-{_slug(reason)}-{uuid.uuid4().hex[:8]}"
            archive = self.root / f"{name}.tar.gz"
            checksum_file = self.root / f"{name}.tar.gz.sha256"
            manifest_file = self.root / f"{name}.tar.gz.manifest.json"
            temp_archive = self.root / f".{name}.tar.gz.partial"

            snapshot_dir = Path(
                tempfile.mkdtemp(prefix=".alex-snap-", dir=self.root)
            )
            try:
                entries, file_count, total_bytes, sqlite_files = self._collect_entries(
                    snapshot_dir
                )
                try:
                    with tarfile.open(temp_archive, "w:gz", compresslevel=6) as tar:
                        for source, arcname in entries:
                            tar.add(str(source), arcname=arcname, recursive=False)
                    fsync_file(temp_archive)
                except Exception as exc:
                    raise BackupError(f"cannot write snapshot archive: {exc}") from exc

                digest = _sha256_file(temp_archive)
                try:
                    os.replace(temp_archive, archive)
                except OSError as exc:
                    raise BackupError(f"cannot publish snapshot {archive}: {exc}") from exc

                checksum_text = f"{digest}  {archive.name}\n"
                manifest = {
                    "format": 1,
                    "name": name,
                    "archive": archive.name,
                    "sha256": digest,
                    "created_at": _utc_now(),
                    "reason": reason,
                    "trigger": trigger,
                    "project_root": str(self.config.project_root),
                    "file_count": file_count,
                    "total_bytes": total_bytes,
                    "sqlite_snapshots": sqlite_files,
                    "excluded_patterns": list(self.config.backup_excludes),
                    "hostname": os.uname().nodename,
                }
                atomic_write_text(checksum_file, checksum_text)
                atomic_write_text(
                    manifest_file,
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                )

                verification = self._verify_archive(
                    archive, expected_sha256=digest, expected_files=file_count
                )
                if not verification.ok:
                    self._remove_snapshot_files(archive, checksum_file, manifest_file)
                    raise BackupError(
                        f"snapshot failed verification: {verification.message}"
                    )

                record = BackupRecord(
                    name=name,
                    archive=archive,
                    checksum_file=checksum_file,
                    manifest_file=manifest_file,
                    sha256=digest,
                    created_at=manifest["created_at"],
                    reason=reason,
                    trigger=trigger,
                    file_count=file_count,
                    total_bytes=total_bytes,
                )
                self._audit_event(
                    "backup_created",
                    name=name,
                    sha256=digest,
                    files=file_count,
                    bytes=total_bytes,
                    reason=reason,
                    trigger=trigger,
                )
                self._prune_locked(self.config.backup_retention)
                return record
            finally:
                shutil.rmtree(snapshot_dir, ignore_errors=True)
                try:
                    temp_archive.unlink(missing_ok=True)
                except OSError:
                    pass

    # ── single-path snapshots ─────────────────────────────────────────────────

    def create_path_snapshot(
        self,
        target: str | Path,
        reason: str,
        *,
        trigger: str = "automatic-file",
    ) -> BackupRecord | None:
        """Archive one file or directory before it is modified or deleted.

        Project-wide snapshots protect Alex's own code; this covers documents
        and folders in the user's working directories, which are not part of
        the project tree.  Returns ``None`` when the target does not exist
        (a brand-new file has no previous state to preserve).
        """
        source = Path(target).expanduser()
        if not source.exists():
            return None
        source = source.resolve(strict=False)
        if source.is_symlink():
            raise BackupError(f"refusing to snapshot a symlink: {source}")
        if not (source.is_file() or source.is_dir()):
            raise BackupError(f"refusing to snapshot a special file: {source}")

        reason = _clean_reason(reason)
        paths_root = self.root / _PATH_BACKUP_DIR
        ensure_private_dir(self.root)
        ensure_private_dir(paths_root)

        estimate = self._path_size(source)
        if estimate > self.config.max_path_snapshot_bytes:
            raise BackupError(
                f"target is too large to snapshot safely: {estimate} bytes "
                f"(limit {self.config.max_path_snapshot_bytes}). Move it "
                "manually or raise ALEX_MAX_PATH_SNAPSHOT_BYTES."
            )

        with self._exclusive_lock():
            self._check_free_space(estimate)
            stamp = _utc_stamp()
            name = f"alex-path-{stamp}-{_slug(reason)}-{uuid.uuid4().hex[:8]}"
            archive = paths_root / f"{name}.tar.gz"
            checksum_file = paths_root / f"{name}.tar.gz.sha256"
            manifest_file = paths_root / f"{name}.tar.gz.manifest.json"
            temp_archive = paths_root / f".{name}.tar.gz.partial"

            try:
                try:
                    with tarfile.open(temp_archive, "w:gz", compresslevel=6) as tar:
                        tar.add(
                            str(source),
                            arcname=source.name,
                            recursive=True,
                            filter=_safe_tarinfo,
                        )
                    fsync_file(temp_archive)
                except (OSError, tarfile.TarError) as exc:
                    raise BackupError(f"cannot snapshot {source}: {exc}") from exc

                digest = _sha256_file(temp_archive)
                with tarfile.open(temp_archive, "r:gz") as tar:
                    file_count = sum(1 for item in tar if not item.isdir())

                try:
                    os.replace(temp_archive, archive)
                except OSError as exc:
                    raise BackupError(f"cannot publish snapshot {archive}: {exc}") from exc

                manifest = {
                    "format": 1,
                    "kind": "path",
                    "name": name,
                    "archive": archive.name,
                    "source": str(source),
                    "sha256": digest,
                    "created_at": _utc_now(),
                    "reason": reason,
                    "trigger": trigger,
                    "is_dir": source.is_dir(),
                    "file_count": file_count,
                    "total_bytes": estimate,
                }
                atomic_write_text(
                    checksum_file, f"{digest}  {archive.name}\n"
                )
                atomic_write_text(
                    manifest_file,
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                )

                verification = self._verify_archive(
                    archive, expected_sha256=digest, expected_files=file_count
                )
                if not verification.ok:
                    self._remove_snapshot_files(archive, checksum_file, manifest_file)
                    raise BackupError(
                        f"path snapshot failed verification: {verification.message}"
                    )

                record = BackupRecord(
                    name=name,
                    archive=archive,
                    checksum_file=checksum_file,
                    manifest_file=manifest_file,
                    sha256=digest,
                    created_at=manifest["created_at"],
                    reason=reason,
                    trigger=trigger,
                    file_count=file_count,
                    total_bytes=estimate,
                )
                self._audit_event(
                    "path_snapshot_created",
                    name=name,
                    source=str(source),
                    sha256=digest,
                    files=file_count,
                    bytes=estimate,
                    reason=reason,
                )
                self._prune_paths_locked(self.config.backup_retention)
                return record
            finally:
                try:
                    temp_archive.unlink(missing_ok=True)
                except OSError:
                    pass

    def list_path_snapshots(self) -> list[BackupRecord]:
        """Return single-path snapshots newest-first."""
        paths_root = self.root / _PATH_BACKUP_DIR
        ensure_private_dir(paths_root)
        return self._list_paths_locked()

    def _list_paths_locked(self) -> list[BackupRecord]:
        paths_root = self.root / _PATH_BACKUP_DIR
        records: list[BackupRecord] = []
        for manifest_path in paths_root.glob(f"{_PATH_BACKUP_GLOB}.manifest.json"):
            if manifest_path.is_symlink():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                name = str(data["name"])
                archive = paths_root / str(data["archive"])
                record = BackupRecord(
                    name=name,
                    archive=archive,
                    checksum_file=paths_root / f"{name}.tar.gz.sha256",
                    manifest_file=manifest_path,
                    sha256=str(data.get("sha256", "")),
                    created_at=str(data.get("created_at", "")),
                    reason=str(data.get("reason", "")),
                    trigger=str(data.get("trigger", "")),
                    file_count=int(data.get("file_count", 0)),
                    total_bytes=int(data.get("total_bytes", 0)),
                )
                if record.archive.is_file() and not record.archive.is_symlink():
                    records.append(record)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def _prune_paths_locked(self, keep: int) -> list[str]:
        records = self._list_paths_locked()
        removed: list[str] = []
        for record in records[keep:]:
            self._remove_snapshot_files(
                record.archive, record.checksum_file, record.manifest_file
            )
            removed.append(record.name)
            self._audit_event("path_snapshot_pruned", name=record.name)
        return removed

    @staticmethod
    def _path_size(target: Path) -> int:
        if target.is_file():
            try:
                return target.stat().st_size
            except OSError:
                return 0
        total = 0
        for current, _dirnames, filenames in os.walk(target, followlinks=False):
            for filename in filenames:
                path = Path(current) / filename
                if path.is_symlink():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    # ── collection ────────────────────────────────────────────────────────────

    def _collect_entries(
        self, snapshot_dir: Path
    ) -> tuple[list[tuple[Path, str]], int, int, list[str]]:
        root = self.config.project_root
        entries: list[tuple[Path, str]] = []
        file_count = 0
        total_bytes = 0
        sqlite_files: list[str] = []

        for current, dirnames, filenames in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            rel_dir = current_path.relative_to(root)
            kept_dirs: list[str] = []

            for dirname in sorted(dirnames):
                path = current_path / dirname
                rel = rel_dir / dirname
                if self._excluded(rel, is_dir=True):
                    continue
                entries.append((path, rel.as_posix()))
                if path.is_symlink():
                    # A symlink to a directory is archived as a symlink, not
                    # walked.  Count it like a file entry so verification and
                    # the manifest agree.
                    file_count += 1
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for filename in sorted(filenames):
                path = current_path / filename
                rel = rel_dir / filename
                if self._excluded(rel, is_dir=False):
                    continue

                if (
                    not path.is_symlink()
                    and path.suffix.lower() in _SQLITE_SUFFIXES
                    and self._is_sqlite(path)
                ):
                    snapshot = snapshot_dir / f"{uuid.uuid4().hex}-{filename}"
                    try:
                        self._snapshot_sqlite(path, snapshot)
                    except BackupError:
                        raise
                    except Exception as exc:
                        # A .db file that looked like SQLite but could not be
                        # copied through the API is preserved byte-for-byte.
                        self._audit_event(
                            "backup_sqlite_fallback",
                            path=rel.as_posix(),
                            error=str(exc),
                        )
                        entries.append((path, rel.as_posix()))
                        total_bytes += path.stat().st_size
                    else:
                        entries.append((snapshot, rel.as_posix()))
                        total_bytes += snapshot.stat().st_size
                        sqlite_files.append(rel.as_posix())
                else:
                    entries.append((path, rel.as_posix()))
                    try:
                        total_bytes += path.stat().st_size
                    except OSError:
                        pass
                file_count += 1

        return entries, file_count, total_bytes, sqlite_files

    def _excluded(self, rel: PurePosixPath, *, is_dir: bool) -> bool:
        rel_text = rel.as_posix()
        for pattern in self.config.backup_excludes:
            if fnmatch.fnmatch(rel_text, pattern):
                return True
            if fnmatch.fnmatch(rel.name, pattern):
                return True
            if is_dir and fnmatch.fnmatch(rel_text + "/", pattern):
                return True
        return False

    @staticmethod
    def _is_sqlite(path: Path) -> bool:
        """True when the file starts with the SQLite database header."""
        try:
            with open(path, "rb") as fh:
                return fh.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
        except OSError:
            return False

    @staticmethod
    def _snapshot_sqlite(source: Path, destination: Path) -> None:
        """Copy a live SQLite DB through the backup API."""
        src = None
        dst = None
        try:
            src = sqlite3.connect(str(source), timeout=15)
            dst = sqlite3.connect(str(destination))
            src.backup(dst)
            dst.commit()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"not a readable SQLite database: {source}: {exc}") from exc
        finally:
            if dst is not None:
                dst.close()
            if src is not None:
                src.close()
        os.chmod(destination, 0o600)

    # ── verification / listing ────────────────────────────────────────────────

    def verify(self, target: BackupRecord | str | Path) -> BackupVerification:
        """Verify checksum and archive structure of a snapshot."""
        record = target if isinstance(target, BackupRecord) else self._record_for(target)
        result = self._verify_archive(
            record.archive,
            expected_sha256=record.sha256 or None,
            expected_files=record.file_count or None,
        )
        self._audit_event(
            "backup_verified" if result.ok else "backup_verify_failed",
            name=record.name,
            message=result.message,
        )
        return result

    def _verify_archive(
        self,
        archive: Path,
        *,
        expected_sha256: str | None,
        expected_files: int | None,
    ) -> BackupVerification:
        if not archive.is_file() or archive.is_symlink():
            return BackupVerification(False, "", 0, f"archive missing: {archive}")
        try:
            digest = _sha256_file(archive)
        except OSError as exc:
            return BackupVerification(False, "", 0, f"cannot read archive: {exc}")
        if expected_sha256 and digest != expected_sha256:
            return BackupVerification(False, digest, 0, "SHA-256 mismatch")

        count = 0
        seen: set[str] = set()
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar:
                    name = member.name
                    pure = PurePosixPath(name)
                    if pure.is_absolute() or ".." in pure.parts:
                        return BackupVerification(
                            False, digest, count, f"unsafe archive member: {name}"
                        )
                    if name in seen:
                        return BackupVerification(
                            False, digest, count, f"duplicate archive member: {name}"
                        )
                    seen.add(name)
                    if member.isdev() or member.isfifo():
                        return BackupVerification(
                            False, digest, count, f"special file in archive: {name}"
                        )
                    if not member.isdir():
                        count += 1
        except (OSError, tarfile.TarError) as exc:
            return BackupVerification(False, digest, count, f"unreadable archive: {exc}")

        if expected_files is not None and count != expected_files:
            return BackupVerification(
                False,
                digest,
                count,
                f"file count mismatch (expected {expected_files}, found {count})",
            )
        return BackupVerification(True, digest, count, "ok")

    def list_backups(self) -> list[BackupRecord]:
        """Return snapshots newest-first, skipping malformed manifests."""
        ensure_private_dir(self.root)
        return self._list_locked()

    def _list_locked(self) -> list[BackupRecord]:
        records: list[BackupRecord] = []
        for manifest_path in self.root.glob("alex-*.tar.gz.manifest.json"):
            if manifest_path.is_symlink():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                archive = self.root / str(data["archive"])
                name = str(data["name"])
                record = BackupRecord(
                    name=name,
                    archive=archive,
                    checksum_file=self.root / f"{name}.tar.gz.sha256",
                    manifest_file=manifest_path,
                    sha256=str(data.get("sha256", "")),
                    created_at=str(data.get("created_at", "")),
                    reason=str(data.get("reason", "")),
                    trigger=str(data.get("trigger", "")),
                    file_count=int(data.get("file_count", 0)),
                    total_bytes=int(data.get("total_bytes", 0)),
                )
                if record.archive.is_file() and not record.archive.is_symlink():
                    records.append(record)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def _record_for(self, target: str | Path) -> BackupRecord:
        """Find a project or single-path snapshot by name or archive path."""
        raw = Path(target).expanduser()
        if not raw.is_absolute():
            raw = self.root / raw

        names = {
            str(target),
            raw.name,
            raw.name.removesuffix(".tar.gz"),
            raw.name.removesuffix(".sha256"),
        }
        candidates = {
            raw,
            self.root / raw.name,
            self.root / _PATH_BACKUP_DIR / raw.name,
        }
        for record in self._list_locked() + self._list_paths_locked():
            if (
                record.archive in candidates
                or record.archive.name in names
                or record.name in names
            ):
                return record
        raise BackupError(f"unknown backup: {target}")

    # ── pruning / restore ─────────────────────────────────────────────────────

    def prune(self, *, keep: int | None = None) -> list[str]:
        """Delete snapshots older than ``keep`` (newest is always retained)."""
        keep = self.config.backup_retention if keep is None else int(keep)
        if keep < 1:
            raise BackupError("keep must be at least 1")
        ensure_private_dir(self.root)
        with self._exclusive_lock():
            return self._prune_locked(keep)

    def _prune_locked(self, keep: int) -> list[str]:
        records = self._list_locked()
        removed: list[str] = []
        for record in records[keep:]:
            self._remove_snapshot_files(
                record.archive, record.checksum_file, record.manifest_file
            )
            removed.append(record.name)
            self._audit_event("backup_pruned", name=record.name)
        return removed

    def _remove_snapshot_files(self, *paths: Path) -> None:
        for path in paths:
            try:
                if path.is_symlink():
                    path.unlink()
                elif path.is_file():
                    path.unlink()
            except OSError:
                continue

    def restore(
        self,
        target: BackupRecord | str | Path,
        destination: str | Path,
        *,
        confirm: str,
        overwrite: bool = False,
    ) -> Path:
        """Extract a verified snapshot into an empty destination directory.

        ``confirm`` must be exactly ``"RESTORE"``.  In-place restoration of a
        live non-empty project is intentionally refused; stop the service,
        move the old tree aside, and restore into the empty path.
        """
        if confirm != "RESTORE":
            raise BackupError('restore requires confirm="RESTORE"')
        record = target if isinstance(target, BackupRecord) else self._record_for(target)
        verification = self.verify(record)
        if not verification.ok:
            raise BackupError(f"refusing to restore an invalid backup: {verification.message}")

        dest = Path(destination).expanduser().resolve(strict=False)
        if dest.exists():
            if dest.is_symlink() or not dest.is_dir():
                raise BackupError(f"restore destination is not a directory: {dest}")
            if any(dest.iterdir()) and not overwrite:
                raise BackupError(f"restore destination is not empty: {dest}")

        parent = dest.parent
        if not parent.is_dir():
            raise BackupError(f"restore parent does not exist: {parent}")
        temp = Path(tempfile.mkdtemp(prefix=".alex-restore-", dir=parent))
        try:
            try:
                with tarfile.open(record.archive, "r:gz") as tar:
                    tar.extractall(path=temp, filter="data")
            except (OSError, tarfile.TarError) as exc:
                raise BackupError(f"cannot extract snapshot: {exc}") from exc

            if dest.exists():
                if any(dest.iterdir()) and not overwrite:
                    raise BackupError(f"restore destination is not empty: {dest}")
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            os.replace(temp, dest)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)

        self._audit_event("backup_restored", name=record.name, destination=str(dest))
        return dest

    # ── infrastructure ────────────────────────────────────────────────────────

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Serialise snapshots across threads and processes."""
        ensure_private_dir(self.root)
        lock_path = self.root / ".backup.lock"
        with self._thread_lock:
            try:
                fd = os.open(
                    lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
                )
            except OSError as exc:
                raise BackupError(f"cannot open backup lock: {exc}") from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _check_free_space(self, estimate: int | None = None) -> None:
        try:
            usage = shutil.disk_usage(self.root)
            if estimate is None:
                estimate = self._estimate_project_bytes()
            required = int(estimate * 1.15) + 64 * 1024 * 1024
            if usage.free < required:
                raise BackupError(
                    f"not enough free space for a snapshot: need about "
                    f"{required} bytes, {usage.free} available"
                )
        except BackupError:
            raise
        except OSError as exc:
            raise BackupError(f"cannot check backup disk space: {exc}") from exc

    def _estimate_project_bytes(self) -> int:
        total = 0
        for current, dirnames, filenames in os.walk(
            self.config.project_root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            rel_dir = current_path.relative_to(self.config.project_root)
            dirnames[:] = [
                name
                for name in dirnames
                if not self._excluded(rel_dir / name, is_dir=True)
                and not (current_path / name).is_symlink()
            ]
            for filename in filenames:
                rel = rel_dir / filename
                if self._excluded(rel, is_dir=False):
                    continue
                try:
                    total += (current_path / filename).stat().st_size
                except OSError:
                    pass
        return total

    def _audit_event(self, event: str, **fields: Any) -> None:
        try:
            log = self._audit or get_audit("backup", self.config)
            log.write(event, **fields)
        except (AuditError, OSError) as exc:
            # A backup must not be lost because the audit sink is down.
            import logging

            logging.getLogger("alex.controller.backup").error(
                "AUDIT WRITE FAILED for %s: %s", event, exc
            )


_MANAGER: BackupManager | None = None


def get_backup_manager() -> BackupManager:
    """Return the process-wide backup manager."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = BackupManager(get_config())
    return _MANAGER


def reset_backup_manager() -> None:
    global _MANAGER
    _MANAGER = None
