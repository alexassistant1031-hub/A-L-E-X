"""
tools/files/tool.py

Create, read, edit, list, copy, move and delete files and directories.

Safety model (Phase 0 + Phase 1 of the self-modification work):

  - Reads are unrestricted, as before.
  - Writes are allowed **only** inside ``<project>/staging/`` and the user's
    configured working directories (``~/Documents``, ``~/Desktop``, ...).
  - Every other project path is read-only: core code, ``tools/``, ``memory/``,
    ``controller/``, backups, audit logs and the trash directory.
  - Credential material (``.env``, private keys, ``.ssh``, ...) is never
    writable even inside an allowed working directory.
  - Before a mutation, :class:`controller.ModificationController` creates a
    verified snapshot: a full project backup for project paths, or a snapshot
    of the affected file/directory for external working-directory paths.
  - Delete is a soft delete into ``~/.alex_trash`` and cannot remove an allowed
    root or a controller directory.
  - Every attempt is written to a hash-chained audit log owned by the
    controller (``/var/log/alex-controller/file.jsonl`` by default), not to a
    project-local file the agent can edit.

The policy itself lives in :mod:`controller.guard`; the backup-first mutation
pipeline lives in :mod:`controller.modification`.  This module is the thin
agent-facing adapter around both.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import settings
from controller import (
    BackupError,
    ControllerConfig,
    ControllerError,
    ModificationController,
    PathGuardError,
    SecureAuditLog,
    WritePathGuard,
    get_audit,
    get_config,
)

from ..base import BaseTool

logger = logging.getLogger("alex.tools.file")

# Actions that change the filesystem and therefore need a writable target,
# a safety snapshot and a mandatory audit record.
_MUTATING_ACTIONS = frozenset(
    {"write", "append", "delete", "mkdir", "copy", "move"}
)

# Read-only actions whose result should not be copied into the audit log
# (a read result can contain the entire file, including private data).
_CONTENT_ACTIONS = frozenset({"read", "list", "exists", "info"})


def _summarize(action: str, result: str) -> str:
    """Return an audit-safe description of an operation result."""
    if action in _CONTENT_ACTIONS:
        return "ok"
    return result[:200]


class FileTool(BaseTool):
    """Filesystem tool with a deny-by-default write policy."""

    name = "file"
    description = (
        "FILESYSTEM TOOL — read, write, edit, append, delete, list, create "
        "directories, copy, move, and inspect files. Writing is limited to "
        "the project's staging/ directory and the user's working folders; "
        "application code and backups are read-only."
    )
    parameters = {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "read", "write", "append", "delete", "list",
                    "mkdir", "copy", "move", "exists", "info",
                ],
                "description": (
                    "'read' = read file contents, 'write' = create/overwrite a "
                    "file, 'append' = add text to a file, 'delete' = move a "
                    "file/folder to trash, 'list' = list a directory, 'mkdir' = "
                    "create a directory, 'copy' / 'move' = copy or move, "
                    "'exists' = check a path, 'info' = size/dates/permissions."
                ),
            },
            "path": {
                "type": "string",
                "description": "File or directory path (absolute, ~, or relative to the project).",
            },
            "content": {
                "type": "string",
                "description": "Text to write or append (for 'write' and 'append').",
                "default": "",
            },
            "destination": {
                "type": "string",
                "description": "Destination path (for 'copy' and 'move').",
                "default": "",
            },
        },
        "required": ["action", "path"],
    }

    def __init__(
        self,
        *,
        config: ControllerConfig | None = None,
        guard: WritePathGuard | None = None,
        audit: SecureAuditLog | None = None,
        modifications: ModificationController | None = None,
    ) -> None:
        # Dependencies are injectable so tests can use temporary directories
        # without touching the real /var/backups or /var/log locations.
        self.config = config or get_config()
        self.guard = guard or WritePathGuard(self.config)
        self._audit = audit
        self.modifications = modifications or ModificationController(
            self.config, guard=self.guard, audit=audit
        )

    # ── audit helpers ─────────────────────────────────────────────────────────

    def _audit_log(self) -> SecureAuditLog:
        return self._audit or get_audit("file", self.config)

    def _record(
        self,
        event: str,
        *,
        action: str,
        path: Path,
        destination: Path | str | None = None,
        result: str = "",
        required: bool = False,
    ) -> bool:
        """Append an audit record and return whether it succeeded.

        ``required=True`` is used *before* a mutation: if the secure log is
        unavailable the operation is refused before anything is touched.
        Read-only actions and post-mutation records are best-effort so a
        broken audit sink can never hide an already-applied change.
        """
        fields = {
            "action": action,
            "path": str(path),
            "destination": str(destination) if destination else "",
            "result": _summarize(action, result),
        }
        try:
            self._audit_log().write(event, **fields)
            return True
        except (ControllerError, OSError) as exc:
            if required:
                # PathGuardError subclasses PermissionError, so the caller
                # reports this as a normal policy denial.
                raise PathGuardError(
                    str(path),
                    f"secure audit log is unavailable: {exc}",
                    allowed=self.guard.describe_allowed(),
                ) from exc
            logger.critical("Secure file audit write failed: %s", exc)
            return False
        except Exception as exc:  # defensive: audit must not crash the tool
            if required:
                raise PathGuardError(
                    str(path),
                    f"secure audit log is unavailable: {exc}",
                    allowed=self.guard.describe_allowed(),
                ) from exc
            logger.critical("Secure file audit write failed: %s", exc)
            return False

    def _finish(
        self,
        action: str,
        path: Path,
        result: str,
        *,
        destination: Path | None = None,
        mutating: bool = False,
    ) -> str:
        """Record the outcome; warn loudly if a completed change was not logged."""
        event = "file_change_completed" if mutating else "file_read"
        if self._record(
            event,
            action=action,
            path=path,
            destination=destination,
            result=result,
        ):
            return result
        if mutating:
            return (
                f"{result}\n\nWARNING: the change was applied, but the secure "
                "audit log could not be updated. Stop and check the controller "
                "audit storage."
            )
        return result

    # ── backup first ──────────────────────────────────────────────────────────

    def _prepare(
        self, action: str, paths: list[Path], *, snapshot_external: bool = True
    ) -> None:
        """Create the safety snapshot required before a mutation.

        Project paths always trigger a fresh full project backup; external
        paths trigger a snapshot of that file or directory.  A snapshot
        failure aborts the mutation.
        """
        # Validate policy *before* creating a snapshot: a denied write must
        # not leave a useless backup behind.
        if action in ("write", "append"):
            self.guard.require_writable(paths[0])
        elif action == "mkdir":
            self.guard.require_directory(paths[0], create=False)
        elif action == "delete":
            self.guard.require_deletable(paths[0])
        elif action == "move":
            self.guard.require_deletable(paths[0])
            self.guard.require_writable(paths[1])
        elif action == "copy":
            self.guard.require_writable(paths[0])

        snapshot = self.modifications.ensure_snapshot(
            action,
            paths,
            reason=f"agent {action}",
            snapshot_external=snapshot_external,
            # The agent-facing tool always takes a fresh snapshot for each
            # project mutation; coalescing is available to other callers.
            coalesce=False,
        )
        # Record which snapshots protect this change.  This also fails closed:
        # if the audit log is unavailable we have not mutated anything yet.
        self.modifications.record(
            "safety_snapshot",
            action=action,
            paths=sorted(snapshot.names),
            reused=snapshot.reused,
        )

    # ── entry point ───────────────────────────────────────────────────────────

    async def execute(
        self, action: str, path: str, content: str = "", destination: str = ""
    ) -> str:
        action = action.strip().lower()
        handlers = {
            "read": self._read,
            "write": self._write,
            "append": self._append,
            "delete": self._delete,
            "list": self._list,
            "mkdir": self._mkdir,
            "copy": self._copy,
            "move": self._move,
            "exists": self._exists,
            "info": self._info,
        }

        handler = handlers.get(action)
        if handler is None:
            return f"Unknown action: {action}"

        # Resolve relative paths against the project root.  The guard also
        # resolves symlinks, so a link inside staging cannot escape policy.
        # Delete/move validate the link itself, not the file it points at.
        if action in ("delete", "move"):
            target = self.guard.resolve_entry(path)
        else:
            target = self.guard.resolve(path)
        mutating = action in _MUTATING_ACTIONS

        try:
            if action in ("copy", "move"):
                if not destination.strip():
                    return f"Action '{action}' requires a destination path."
                dest = self.guard.resolve(destination)
                if mutating:
                    # Fail closed when the audit sink is down, before snapshot
                    # work or filesystem changes begin.
                    self._record(
                        "file_change_requested",
                        action=action,
                        path=target,
                        destination=dest,
                        required=True,
                    )
                result = await handler(target, dest)
                return self._finish(
                    action, target, result, destination=dest, mutating=mutating
                )

            if action in ("write", "append"):
                if mutating:
                    self._record(
                        "file_change_requested",
                        action=action,
                        path=target,
                        required=True,
                    )
                result = await handler(target, content)
                return self._finish(action, target, result, mutating=mutating)

            if mutating:
                self._record(
                    "file_change_requested",
                    action=action,
                    path=target,
                    required=True,
                )
            result = await handler(target)
            return self._finish(action, target, result, mutating=mutating)

        except PermissionError as exc:
            self._record(
                "file_denied" if mutating else "file_error",
                action=action,
                path=target,
                result=str(exc),
            )
            return f"Permission denied: {exc}"
        except BackupError as exc:
            self._record(
                "file_change_aborted",
                action=action,
                path=target,
                result=str(exc),
            )
            return f"Backup failed; refusing to modify {target}: {exc}"
        except ControllerError as exc:
            self._record(
                "file_error", action=action, path=target, result=str(exc)
            )
            return f"Controller error: {exc}"
        except FileNotFoundError as exc:
            self._record(
                "file_error", action=action, path=target, result=str(exc)
            )
            return f"File not found: {exc}"
        except IsADirectoryError as exc:
            self._record(
                "file_error", action=action, path=target, result=str(exc)
            )
            return f"Path is a directory: {exc}"
        except UnicodeDecodeError:
            self._record(
                "file_error",
                action=action,
                path=target,
                result="binary file",
            )
            return f"Cannot read binary file as text: {target}"
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("File tool failed")
            self._record(
                "file_error", action=action, path=target, result=str(exc)
            )
            return f"File error: {exc}"

    # ── read-only actions ─────────────────────────────────────────────────────

    async def _read(self, path: Path) -> str:
        replacement = settings.retired_reply_redirect(path)
        if replacement is not None:
            return (
                f"That reply path is retired: {path}. "
                f"Read {replacement} instead."
            )
        if not path.exists():
            return f"File not found: {path}"
        if path.is_dir():
            return f"Path is a directory, not a file: {path}. Use 'list' to see its contents."

        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        lines = text.split("\n")
        total = len(lines)
        if total > 500:
            body = "\n".join(lines[:500])
            return f"{path} ({total} lines, showing first 500):\n\n{body}"
        return f"{path} ({total} lines):\n\n{text}"

    async def _list(self, path: Path) -> str:
        if not path.exists():
            return f"Path not found: {path}"
        if not path.is_dir():
            return f"Not a directory: {path}"

        entries = await asyncio.to_thread(list, path.iterdir())
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))

        lines = [f"Contents of {path}:"]
        for entry in entries:
            suffix = "/" if entry.is_dir() else ""
            try:
                size = entry.stat().st_size if entry.is_file() else 0
                size_str = f" ({size} bytes)" if size else ""
                lines.append(f"  {entry.name}{suffix}{size_str}")
            except OSError:
                lines.append(f"  {entry.name}{suffix}")

        if len(lines) == 1:
            lines.append("  (empty)")
        return "\n".join(lines)

    async def _exists(self, path: Path) -> str:
        exists = path.exists()
        if not exists:
            return f"Path: {path}\nExists: False\nType: N/A"
        return f"Path: {path}\nExists: True\nType: {'directory' if path.is_dir() else 'file'}"

    async def _info(self, path: Path) -> str:
        if not path.exists():
            return f"Path not found: {path}"
        stat = path.stat()
        created = datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M:%S")
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return "\n".join(
            [
                f"Path: {path}",
                f"Type: {'directory' if path.is_dir() else 'file'}",
                f"Size: {stat.st_size} bytes",
                f"Created: {created}",
                f"Modified: {modified}",
                f"Permissions: {oct(stat.st_mode)[-3:]}",
            ]
        )

    # ── mutating actions (snapshot -> atomic change) ──────────────────────────

    async def _write(self, path: Path, content: str) -> str:
        # Policy first, snapshot second: a denied write must not create a
        # needless full backup.
        self.guard.require_writable(path)
        await asyncio.to_thread(self._prepare, "write", [path])
        await asyncio.to_thread(
            self.modifications.write_text, path, content
        )
        return f"Written {len(content.encode('utf-8'))} bytes to {path}"

    async def _append(self, path: Path, content: str) -> str:
        self.guard.require_writable(path)
        await asyncio.to_thread(self._prepare, "append", [path])
        await asyncio.to_thread(
            self.modifications.write_text, path, content, append=True
        )
        return f"Appended {len(content.encode('utf-8'))} bytes to {path}"

    async def _delete(self, path: Path) -> str:
        if not path.exists() and not path.is_symlink():
            return f"Path not found: {path}"

        self.guard.require_deletable(path)
        # The item itself is preserved in the trash, so an extra copy is not
        # needed for working-directory files.  A project path still triggers a
        # full project snapshot before the move.
        kind = "directory" if path.is_dir() else "file"
        await asyncio.to_thread(
            self._prepare, "delete", [path], snapshot_external=False
        )
        source, trash_target = await asyncio.to_thread(
            self.modifications.delete, path
        )
        return (
            f"Moved {kind} to trash: {source} -> {trash_target}\n"
            f"(Recoverable from {self.config.trash_root}.)"
        )

    async def _mkdir(self, path: Path) -> str:
        self.guard.require_writable(path)
        await asyncio.to_thread(self._prepare, "mkdir", [path])
        await asyncio.to_thread(self.modifications.mkdir, path)
        return f"Created directory: {path}"

    async def _copy(self, src: Path, dst: Path) -> str:
        if not src.exists() and not src.is_symlink():
            return f"Source not found: {src}"
        if src.is_dir() and (dst == src or src in dst.parents):
            return "Refused: cannot copy a directory into itself."

        self.guard.require_safe_source(src)
        self.guard.require_writable(dst)
        # Snapshot the destination's previous state (if any) before copying.
        await asyncio.to_thread(self._prepare, "copy", [dst])
        await asyncio.to_thread(self.modifications.copy, src, dst)
        return f"Copied {src} -> {dst}"

    async def _move(self, src: Path, dst: Path) -> str:
        if not src.exists() and not src.is_symlink():
            return f"Source not found: {src}"
        if src.is_dir() and (dst == src or src in dst.parents):
            return "Refused: cannot move a directory into itself."

        self.guard.require_deletable(src)
        self.guard.require_writable(dst)
        # Both ends matter: the source disappears and an existing destination
        # may be replaced.
        await asyncio.to_thread(self._prepare, "move", [src, dst])
        await asyncio.to_thread(self.modifications.move, src, dst)
        return f"Moved {src} -> {dst}"


def backend_status(
    config: ControllerConfig | None = None,
) -> dict[str, str]:
    """Return the locations used by the file tool."""
    cfg = config or get_config()
    return {
        "staging": str(cfg.staging_root),
        "backups": str(cfg.backup_root),
        "audit": str(cfg.audit_root / "file.jsonl"),
        "trash": str(cfg.trash_root),
    }


__all__ = ["FileTool", "backend_status"]
