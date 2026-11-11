"""
tools/edit/tool.py

Exact-string file editing for Alex, modelled on a patch-style replace.

Why a separate tool
-------------------
``file_operation write`` replaces a whole file, which is easy to get wrong and
easy to clobber.  ``edit_file`` changes only the requested text:

* ``old_string`` must match the file byte-for-byte (UTF-8 text);
* an ambiguous match is refused unless ``replace_all`` is true, so a short
  string cannot silently rewrite several places;
* the file must be a regular, single-link file inside the writable policy;
* a verified safety snapshot is created before the change;
* the intent is written to the controller-owned ``file`` audit stream first,
  and a failed audit write aborts the edit.

No content is copied into audit fields, only sizes and SHA-256 hashes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import stat
from pathlib import Path

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

logger = logging.getLogger("alex.tools.edit")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class EditTool(BaseTool):
    """Replace an exact string in a writable text file."""

    name = "edit_file"
    description = (
        "EDIT TOOL — replace an exact string in a text file. Use this instead "
        "of rewriting a whole file. The old text must match exactly; if it "
        "appears more than once the edit is refused unless replace_all is "
        "true. Every edit creates a verified safety backup first."
    )
    parameters = {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "string",
                "description": "File to edit (absolute, ~, or relative to the project).",
            },
            "old_string": {
                "type": "string",
                "description": "Exact existing text to replace. Include enough context to be unique.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text. Use an empty string to delete the matched text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence instead of requiring a unique match.",
                "default": False,
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(
        self,
        *,
        config: ControllerConfig | None = None,
        guard: WritePathGuard | None = None,
        audit: SecureAuditLog | None = None,
        modifications: ModificationController | None = None,
    ) -> None:
        self.config = config or get_config()
        self.guard = guard or WritePathGuard(self.config)
        self._audit = audit
        self.modifications = modifications or ModificationController(
            self.config, guard=self.guard, audit=audit
        )

    # ── audit ─────────────────────────────────────────────────────────────────

    def _audit_log(self) -> SecureAuditLog:
        return self._audit or get_audit("file", self.config)

    def _record(self, event: str, *, required: bool = False, **fields) -> bool:
        try:
            self._audit_log().write(event, **fields)
            return True
        except (ControllerError, OSError) as exc:
            if required:
                raise PathGuardError(
                    str(fields.get("path", "")),
                    f"secure audit log is unavailable: {exc}",
                    allowed=self.guard.describe_allowed(),
                ) from exc
            logger.critical("Secure edit audit write failed (%s): %s", event, exc)
            return False
        except Exception as exc:
            if required:
                raise PathGuardError(
                    str(fields.get("path", "")),
                    f"secure audit log is unavailable: {exc}",
                    allowed=self.guard.describe_allowed(),
                ) from exc
            logger.critical("Secure edit audit write failed (%s): %s", event, exc)
            return False

    # ── entry point ───────────────────────────────────────────────────────────

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        return await asyncio.to_thread(
            self._edit, path, old_string, new_string, replace_all
        )

    def _edit(
        self, path: str, old_string: str, new_string: str, replace_all: bool
    ) -> str:
        if not (path or "").strip():
            return "Refused: no file path was given."
        if old_string == "":
            return (
                "Refused: old_string must not be empty. To create a file, use "
                "file_operation with action='write'."
            )
        if old_string == new_string:
            return "No change: old_string and new_string are identical."

        target: Path | None = None
        action = "edit"
        try:
            # Policy first: never read or snapshot a path we could not edit.
            target = self.guard.require_writable(path)
            if not target.exists():
                return (
                    f"File not found: {target}. Use file_operation with "
                    "action='write' to create it."
                )
            if target.is_symlink() or not target.is_file():
                return f"Refused: not a regular file: {target}"

            info = target.stat()
            if info.st_nlink > 1:
                return (
                    f"Refused: {target} has {info.st_nlink} hard links. Editing "
                    "it could alter another file through the link; use a "
                    "regular copy instead."
                )
            if not stat.S_ISREG(info.st_mode):
                return f"Refused: not a regular file: {target}"

            raw = target.read_bytes()
            if b"\x00" in raw:
                return f"Refused: {target} looks like a binary file."
            try:
                original = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                return f"Refused: {target} is not valid UTF-8 text: {exc}"
            if len(raw) > self.config.max_write_bytes:
                return (
                    f"Refused: {target} is {len(raw)} bytes; the edit limit is "
                    f"{self.config.max_write_bytes} bytes."
                )

            occurrences = original.count(old_string)
            if occurrences == 0:
                return (
                    f"No match in {target}: old_string was not found. Read the "
                    "file and copy the exact text, including whitespace and "
                    "line endings."
                )
            if occurrences > 1 and not replace_all:
                return (
                    f"Refused: old_string appears {occurrences} times in "
                    f"{target}. Add surrounding context to make it unique, or "
                    "set replace_all=true to change every occurrence."
                )

            if replace_all:
                updated = original.replace(old_string, new_string)
            else:
                updated = original.replace(old_string, new_string, 1)

            data = updated.encode("utf-8")
            if len(data) > self.config.max_write_bytes:
                return (
                    f"Refused: the edited file would be {len(data)} bytes; the "
                    f"limit is {self.config.max_write_bytes} bytes."
                )

            # Record intent before touching disk.  This fails closed.
            self._record(
                "file_edit_requested",
                required=True,
                path=str(target),
                occurrences=occurrences,
                replace_all=bool(replace_all),
                bytes_before=len(raw),
                bytes_after=len(data),
                sha256_before=_sha256(raw),
                sha256_after=_sha256(data),
            )

            snapshot = self.modifications.ensure_snapshot(
                action,
                [target],
                reason=f"agent exact-string edit ({occurrences} match{'es' if occurrences != 1 else ''})",
                coalesce=False,
            )
            self.modifications.record(
                "safety_snapshot",
                action=action,
                paths=sorted(snapshot.names),
                reused=snapshot.reused,
            )

            # Re-read immediately before publishing: if another process
            # changed the file while the snapshot was being taken, the edit
            # would silently overwrite that change.
            try:
                current = target.read_bytes()
            except OSError as exc:
                self._record(
                    "file_edit_aborted",
                    path=str(target),
                    result=f"re-read failed: {exc}"[:200],
                )
                return f"Refused: could not re-read {target} before writing: {exc}"
            if current != raw:
                self._record(
                    "file_edit_aborted",
                    path=str(target),
                    result="file changed while the edit was prepared",
                )
                return (
                    f"Refused: {target} changed while the edit was being "
                    "prepared. Read it again and repeat the edit."
                )

            self.modifications.write_text(target, updated)
        except PathGuardError as exc:
            self._record(
                "file_edit_denied",
                path=str(target or path),
                result=str(exc)[:200],
            )
            return f"Permission denied: {exc}"
        except BackupError as exc:
            self._record(
                "file_edit_aborted",
                path=str(target or path),
                result=str(exc)[:200],
            )
            return (
                f"Backup failed; refusing to edit {target or path}: {exc}"
            )
        except (ControllerError, OSError, UnicodeError) as exc:
            self._record(
                "file_edit_failed",
                path=str(target or path),
                result=str(exc)[:200],
            )
            return f"Could not edit {target or path}: {exc}"

        audited = self._record(
            "file_edit_completed",
            path=str(target),
            occurrences=occurrences,
            replace_all=bool(replace_all),
            bytes_before=len(raw),
            bytes_after=len(data),
            sha256_after=_sha256(data),
            snapshot=sorted(snapshot.names),
        )

        count = occurrences if replace_all else 1
        result = (
            f"Edited {target}: replaced {count} "
            f"{'occurrences' if count != 1 else 'occurrence'} "
            f"({len(raw)} -> {len(data)} bytes)."
        )
        if snapshot.project is not None or snapshot.paths:
            result += f" Backup: {', '.join(sorted(snapshot.names))}."
        if not audited:
            result += (
                "\n\nWARNING: the edit was applied, but the secure audit log "
                "could not be updated. Stop and check the controller audit "
                "storage."
            )
        return result


def backend_status(config: ControllerConfig | None = None) -> dict[str, str]:
    cfg = config or get_config()
    return {
        "tool": "edit_file",
        "audit_log": str(cfg.audit_root / "file.jsonl"),
        "writable": ", ".join(WritePathGuard(cfg).describe_allowed()),
    }


__all__ = ["EditTool", "backend_status"]
