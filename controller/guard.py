"""Write-path policy for the agent-facing filesystem tool.

Policy (deny always wins):

    allowed   <project>/staging/** and the configured user working roots
    denied    every other path inside the project (core code, tools, memory,
              controller, live extensions), the backup root, the audit root,
              the trash root, credential paths, and anything else

The guard resolves symlinks before deciding, so a link inside staging cannot
be used to reach ``/etc`` or the live code.  ``open_for_write`` additionally
uses ``O_NOFOLLOW`` and re-checks the opened file descriptor through
``/proc/self/fd`` to close the common TOCTOU gap.
"""

from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path

from .config import (
    SENSITIVE_DIR_NAMES,
    SENSITIVE_FILE_PATTERNS,
    SENSITIVE_PATH_FRAGMENTS,
    ControllerConfig,
)
from .errors import PathGuardError


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class WritePathGuard:
    """Validate every mutating filesystem operation before it happens."""

    def __init__(self, config: ControllerConfig) -> None:
        self.config = config

    # ── public API ────────────────────────────────────────────────────────────

    def resolve(self, value: str | Path) -> Path:
        """Resolve a user/model path against the project root."""
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.config.project_root / path
        return path.resolve(strict=False)

    def resolve_entry(self, value: str | Path) -> Path:
        """Resolve the parent but keep the final component intact.

        This is what delete/move need: removing a symlink must validate the
        link's own location, not the file it happens to point at.
        """
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.config.project_root / path
        return path.parent.resolve(strict=False) / path.name

    def describe_allowed(self) -> tuple[str, ...]:
        return tuple(str(p) for p in (self.config.staging_root, *self.config.allowed_write_roots))

    def is_sensitive(self, value: str | Path) -> bool:
        path = self.resolve(value)
        parts = [part.lower() for part in path.parts]
        if any(part in SENSITIVE_DIR_NAMES for part in parts):
            return True
        joined = "/".join(parts)
        if any(fragment in joined for fragment in SENSITIVE_PATH_FRAGMENTS):
            return True
        name = path.name.lower()
        return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_FILE_PATTERNS)

    def explain(self, value: str | Path) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` without raising."""
        path = self.resolve(value)
        return self._classify(path, allow_root=False)

    def require_writable(self, value: str | Path, *, action: str = "write") -> Path:
        """Return the resolved path or raise :class:`PathGuardError`."""
        path = self.resolve(value)
        allowed, reason = self._classify(path, allow_root=False)
        if not allowed:
            raise PathGuardError(str(path), reason, allowed=self.describe_allowed())
        return path

    def require_directory(self, value: str | Path, *, create: bool = False) -> Path:
        """Validate a directory target (``mkdir`` or a file's parent)."""
        path = self.resolve(value)
        allowed, reason = self._classify(path, allow_root=True)
        if not allowed:
            raise PathGuardError(str(path), reason, allowed=self.describe_allowed())
        if create:
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            except OSError as exc:
                raise PathGuardError(str(path), f"cannot create directory: {exc}") from exc
            if path.is_symlink() or not path.is_dir():
                raise PathGuardError(str(path), "path is not a real directory")
            # Re-check after creation in case a parent was swapped.
            allowed, reason = self._classify(path.resolve(strict=False), allow_root=True)
            if not allowed:
                raise PathGuardError(str(path), reason, allowed=self.describe_allowed())
        return path

    def require_deletable(self, value: str | Path) -> Path:
        """Validate the source entry of a delete/move operation."""
        path = self.resolve_entry(value)
        allowed, reason = self._classify(path, allow_root=False)
        if not allowed:
            raise PathGuardError(str(path), reason, allowed=self.describe_allowed())
        protected = (
            self.config.project_root,
            self.config.staging_root,
            self.config.backup_root,
            self.config.audit_root,
            self.config.trash_root,
            *self.config.allowed_write_roots,
        )
        if any(path == root for root in protected):
            raise PathGuardError(
                str(path),
                "cannot delete an allowed root or controller directory",
                allowed=self.describe_allowed(),
            )
        return path

    def require_safe_source(self, value: str | Path) -> Path:
        """Validate a copy/move source, refusing credential material."""
        path = self.resolve(value)
        if self.is_sensitive(path):
            raise PathGuardError(
                str(path),
                "credential material cannot be copied or moved",
                allowed=self.describe_allowed(),
            )
        if not path.exists():
            raise PathGuardError(str(path), "source does not exist")
        return path

    # ── safe write primitives ─────────────────────────────────────────────────

    def open_for_write(self, value: str | Path, *, append: bool = False):
        """Open a validated path for writing, refusing symlinks.

        Returns a text file object.  The caller must use a ``with`` block.
        """
        path = self.require_writable(value, action="append" if append else "write")
        parent = path.parent
        if not parent.exists():
            self.require_directory(parent, create=True)
        else:
            # Existing parents must still be inside the policy.
            self.require_directory(parent, create=False)

        mode = 0o600
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass

        # Open WITHOUT O_TRUNC: the inode must be inspected before any data is
        # discarded.  O_NONBLOCK stops a FIFO from blocking the open; it has no
        # effect on regular files.
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        if append:
            flags |= os.O_APPEND
        try:
            fd = os.open(path, flags, mode)
        except OSError as exc:
            raise PathGuardError(str(path), f"cannot open for writing: {exc}") from exc

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise PathGuardError(
                    str(path), "refusing to write to a non-regular file"
                )
            # A hard link inside an allowed directory could otherwise write
            # through to a protected file outside it.  The guard resolves
            # paths, but inode identity is the only reliable check.
            if info.st_nlink > 1:
                raise PathGuardError(
                    str(path),
                    f"refusing to write to a file with {info.st_nlink} hard links",
                )
            actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
            actual = actual.resolve(strict=False)
            if actual != path:
                raise PathGuardError(
                    str(path),
                    f"path changed after validation (opened {actual})",
                )
            os.fchmod(fd, mode)
            if not append:
                os.ftruncate(fd, 0)
        except Exception:
            os.close(fd)
            raise

        return os.fdopen(fd, "a" if append else "w", encoding="utf-8", newline="")

    # ── decision logic ────────────────────────────────────────────────────────

    def _classify(self, path: Path, *, allow_root: bool) -> tuple[bool, str]:
        if self.is_sensitive(path):
            return False, "credential or secret material is read-only"

        if _within(path, self.config.staging_root):
            return True, "inside staging"

        if _within(path, self.config.project_root):
            return False, "protected project core (only staging is writable)"

        for label, root in (
            ("backup storage", self.config.backup_root),
            ("controller audit storage", self.config.audit_root),
            ("trash storage", self.config.trash_root),
        ):
            if _within(path, root):
                return False, f"inside {label}"

        for root in self.config.allowed_write_roots:
            if _within(path, root):
                if path == root and not allow_root:
                    return False, "an allowed working root cannot be modified directly"
                return True, "inside an allowed working directory"

        return False, "outside the staging area and allowed working directories"
