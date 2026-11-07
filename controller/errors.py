"""Exceptions used by the self-modification controller.

Keeping these separate makes it easy for callers to distinguish a policy
denial (the agent tried to write somewhere it may not) from an infrastructure
failure (the backup or audit storage is unavailable).
"""

from __future__ import annotations


class ControllerError(RuntimeError):
    """Base class for all controller failures."""


class ControllerStorageError(ControllerError):
    """The controller cannot use its private backup/audit directory."""


class AuditError(ControllerStorageError):
    """The secure audit log is unavailable or failed verification."""


class BackupError(ControllerStorageError):
    """A backup could not be created, verified, pruned, or restored."""


class ModificationError(ControllerError):
    """A privileged live-code modification was refused."""


class PathGuardError(PermissionError):
    """A write was denied by the path policy.

    Subclasses PermissionError so existing ``except PermissionError`` blocks
    keep working while still carrying structured policy details.
    """

    def __init__(self, path: str, reason: str, *, allowed: tuple[str, ...] = ()) -> None:
        self.path = path
        self.reason = reason
        self.allowed = allowed
        hint = f" Allowed write roots: {', '.join(allowed)}." if allowed else ""
        super().__init__(f"Write denied for {path}: {reason}.{hint}")
