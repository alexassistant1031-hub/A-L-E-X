"""Self-modification controller for Alex (Phase 0 + Phase 1).

The controller is the only component allowed to change files on disk.  It
provides:

* **private storage** outside the project tree (backups, audit logs),
* **full project snapshots** with SHA-256 checksums and JSON manifests,
* **a write-path guard** that only permits ``staging/`` and the user's
  working directories,
* **a hash-chained audit log** owned by the controller,
* **a backup-before-modification pipeline** wrapped around every write.

No Git.  No network.  No subprocesses.  Everything is standard-library Python.

Typical use::

    from controller import get_modification_controller

    ctl = get_modification_controller()
    ctl.ensure_snapshot("write", [path])
    ctl.write_text(path, "new contents")

Operators can use ``python -m controller status`` (or the ``cli`` module) to
inspect the policy, list snapshots, verify checksums, and restore a backup.
"""

from __future__ import annotations

from .audit import SecureAuditLog, get_audit, reset_audits
from .backup import (
    BackupManager,
    BackupRecord,
    BackupVerification,
    get_backup_manager,
    reset_backup_manager,
)
from .config import ControllerConfig, get_config, reset_config, set_config
from .errors import (
    AuditError,
    BackupError,
    ControllerError,
    ControllerStorageError,
    ModificationError,
    PathGuardError,
)
from .guard import WritePathGuard
from .modification import (
    ModificationController,
    SafetySnapshot,
    get_modification_controller,
    reset_modification_controller,
)
from .status import controller_status, describe_status

__version__ = "1.0.0"

__all__ = [
    "AuditError",
    "BackupError",
    "BackupManager",
    "BackupRecord",
    "BackupVerification",
    "ControllerConfig",
    "ControllerError",
    "ControllerStorageError",
    "ModificationController",
    "ModificationError",
    "PathGuardError",
    "SafetySnapshot",
    "SecureAuditLog",
    "WritePathGuard",
    "__version__",
    "controller_status",
    "describe_status",
    "get_audit",
    "get_backup_manager",
    "get_config",
    "get_modification_controller",
    "reset_audits",
    "reset_backup_manager",
    "reset_config",
    "reset_modification_controller",
    "set_config",
]
