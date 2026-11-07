"""Configuration for the self-modification controller.

Defaults:

    project root      the directory containing this package
    staging           <project>/staging
    backups           /var/backups/alex
    audit logs        /var/log/alex-controller
    writable roots    ~/Documents, ~/Desktop, ~/Downloads, ~/Projects,
                      ~/Notes, ~/Work

Every path can be overridden with an environment variable, which is mainly
useful for tests and for operators who keep backups on a separate volume.
The project tree itself is never writable by the agent; only ``staging/`` is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BACKUP_ROOT = Path("/var/backups/alex")
DEFAULT_AUDIT_ROOT = Path("/var/log/alex-controller")

# When the system-wide directories cannot be created (for example because the
# service runs unprivileged and the operator has not run setup.sh yet), the
# controller falls back to the XDG state directory.  This is still outside the
# project tree and still private (0700), so the security properties are kept;
# only the location differs.  Set ALEX_STORAGE_FALLBACK=0 to make the
# controller fail closed instead.
FALLBACK_BACKUP_ROOT = Path.home() / ".local" / "state" / "alex" / "backups"
FALLBACK_AUDIT_ROOT = Path.home() / ".local" / "state" / "alex" / "audit"

DEFAULT_WRITE_ROOTS = (
    "~/Documents",
    "~/Desktop",
    "~/Downloads",
    "~/Projects",
    "~/Notes",
    "~/Work",
)

# Reproducible or transient content is skipped by default.  Set
# ALEX_BACKUP_EXCLUDES to a pathsep-separated list to replace this list
# (an empty value means "include everything").
DEFAULT_BACKUP_EXCLUDES = (
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "*.tmp",
    "*.partial",
    "*.swp",
    "*~",
    "*.db-wal",
    "*.db-shm",
)

# Directories that are loaded by the running assistant or define its policy.
# The whole project is protected anyway; this list exists for clear audit
# messages and for future live-extension layouts.
LIVE_EXTENSION_DIRS = ("tools", "memory", "controller", "extensions", "plugins")

# Credential material is never writable, even inside an allowed working root.
SENSITIVE_DIR_NAMES = frozenset({".ssh", ".gnupg", ".aws", ".password-store"})
SENSITIVE_PATH_FRAGMENTS = (".local/share/keyrings",)
SENSITIVE_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".git-credentials",
    "credentials.json",
)

_PACKAGE_ROOT = Path(__file__).resolve().parent


def _resolve(value: str | Path) -> Path:
    """Expand ``~`` and make a path absolute without requiring it to exist."""
    return Path(value).expanduser().resolve(strict=False)


def _within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or lives below it (already resolved)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _env_path(name: str, default: Path | str, *, base: Path | None = None) -> Path:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return _resolve(default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve(strict=False)


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    # An explicitly empty value means "no excludes".
    return tuple(item for item in raw.split(os.pathsep) if item.strip())


def _usable_target(path: Path) -> bool:
    """True when ``path`` can be created or already is a writable directory.

    Only the nearest existing ancestor is tested, so this function does not
    create anything and is safe to call while building the configuration.
    """
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False
        probe = probe.parent
    if probe.is_symlink() or not probe.is_dir():
        return False
    try:
        if probe.stat().st_uid != os.geteuid():
            return False
    except OSError:
        return False
    return os.access(probe, os.W_OK | os.X_OK)


def _choose_storage(
    env_name: str, preferred: Path, fallback: Path
) -> tuple[Path, bool]:
    """Return ``(root, is_fallback)`` for a private storage directory.

    An explicit environment variable always wins and is never silently
    replaced.  The default location falls back to the XDG state directory when
    it is not writable, unless ``ALEX_STORAGE_FALLBACK=0``.
    """
    raw = os.getenv(env_name)
    if raw is not None and raw.strip():
        return _resolve(raw), False

    preferred = _resolve(preferred)
    if _usable_target(preferred):
        return preferred, False
    if os.getenv("ALEX_STORAGE_FALLBACK", "1").strip() == "0":
        return preferred, False
    return _resolve(fallback), True


@dataclass(frozen=True)
class ControllerConfig:
    """Immutable, validated policy for backups, guards, and audit logs."""

    project_root: Path = _PACKAGE_ROOT.parent
    staging_root: Path = _PACKAGE_ROOT.parent / "staging"
    backup_root: Path = DEFAULT_BACKUP_ROOT
    audit_root: Path = DEFAULT_AUDIT_ROOT
    allowed_write_roots: tuple[Path, ...] = field(
        default_factory=lambda: tuple(_resolve(p) for p in DEFAULT_WRITE_ROOTS)
    )
    trash_root: Path = Path.home() / ".alex_trash"
    protected_roots: tuple[Path, ...] = ()
    backup_retention: int = 20
    backup_excludes: tuple[str, ...] = DEFAULT_BACKUP_EXCLUDES
    backup_coalesce_seconds: int = 30
    max_write_bytes: int = 10 * 1024 * 1024
    max_path_snapshot_bytes: int = 512 * 1024 * 1024
    audit_max_field_chars: int = 2000
    backup_root_is_fallback: bool = False
    audit_root_is_fallback: bool = False

    def __post_init__(self) -> None:
        project = _resolve(self.project_root)
        staging = _resolve(self.staging_root)
        backup = _resolve(self.backup_root)
        audit = _resolve(self.audit_root)
        trash = _resolve(self.trash_root)
        allowed = tuple(_resolve(p) for p in self.allowed_write_roots)

        if not project.is_dir():
            raise ValueError(f"project root is not a directory: {project}")
        if not _within(staging, project) or staging == project:
            raise ValueError("staging must be a subdirectory of the project")

        for label, private in (("backup", backup), ("audit", audit)):
            if _within(private, project) or _within(project, private):
                raise ValueError(f"{label} root must be outside the project tree")
            if private == staging or _within(private, staging):
                raise ValueError(f"{label} root must not overlap staging")
        if backup == audit or _within(backup, audit) or _within(audit, backup):
            raise ValueError("backup and audit roots must be separate")

        if self.backup_retention < 1:
            raise ValueError("backup_retention must be at least 1")
        if self.backup_coalesce_seconds < 0:
            raise ValueError("backup_coalesce_seconds must not be negative")
        if self.max_write_bytes < 1:
            raise ValueError("max_write_bytes must be positive")
        if self.max_path_snapshot_bytes < 1:
            raise ValueError("max_path_snapshot_bytes must be positive")
        if self.audit_max_field_chars < 128:
            raise ValueError("audit_max_field_chars must be at least 128")

        home = Path.home().resolve()
        for root in allowed:
            if root == Path("/").resolve() or root == home:
                raise ValueError(
                    f"refusing to grant write access to {root}; choose a "
                    "specific working directory instead"
                )
            if root in (project, staging, backup, audit):
                raise ValueError(f"write root overlaps protected storage: {root}")
            if not root.exists():
                # Creating a missing working directory is fine; the guard
                # validates the path again after creation.
                continue

        protected = tuple(_resolve(p) for p in self.protected_roots)
        if not protected:
            protected = (
                project,
                backup,
                audit,
                trash,
                *(project / name for name in LIVE_EXTENSION_DIRS),
            )
        protected = tuple(dict.fromkeys(protected))

        for name, value in (
            ("project_root", project),
            ("staging_root", staging),
            ("backup_root", backup),
            ("audit_root", audit),
            ("allowed_write_roots", allowed),
            ("trash_root", trash),
            ("protected_roots", protected),
        ):
            object.__setattr__(self, name, value)

    @classmethod
    def from_env(cls) -> "ControllerConfig":
        """Build a configuration from ``ALEX_*`` environment variables."""
        project = _env_path("ALEX_PROJECT_ROOT", _PACKAGE_ROOT.parent)
        staging = _env_path("ALEX_STAGING_ROOT", project / "staging", base=project)
        backup, backup_fallback = _choose_storage(
            "ALEX_BACKUP_ROOT", DEFAULT_BACKUP_ROOT, FALLBACK_BACKUP_ROOT
        )
        audit, audit_fallback = _choose_storage(
            "ALEX_AUDIT_ROOT", DEFAULT_AUDIT_ROOT, FALLBACK_AUDIT_ROOT
        )
        trash = _env_path("ALEX_TRASH_ROOT", Path.home() / ".alex_trash")

        roots_raw = os.getenv("ALEX_WRITE_ROOTS")
        if roots_raw is None:
            roots = tuple(_resolve(p) for p in DEFAULT_WRITE_ROOTS)
        else:
            roots = tuple(
                _resolve(item)
                for item in roots_raw.split(os.pathsep)
                if item.strip()
            )

        excludes = _env_list("ALEX_BACKUP_EXCLUDES", DEFAULT_BACKUP_EXCLUDES)
        retention = int(os.getenv("ALEX_BACKUP_RETENTION", "20"))
        coalesce = int(os.getenv("ALEX_BACKUP_COALESCE_SECONDS", "30"))
        max_write = int(os.getenv("ALEX_MAX_WRITE_BYTES", str(10 * 1024 * 1024)))
        max_path_snapshot = int(
            os.getenv("ALEX_MAX_PATH_SNAPSHOT_BYTES", str(512 * 1024 * 1024))
        )
        audit_chars = int(os.getenv("ALEX_AUDIT_MAX_FIELD_CHARS", "2000"))

        return cls(
            project_root=project,
            staging_root=staging,
            backup_root=backup,
            audit_root=audit,
            allowed_write_roots=roots,
            trash_root=trash,
            backup_retention=retention,
            backup_excludes=excludes,
            backup_coalesce_seconds=coalesce,
            max_write_bytes=max_write,
            max_path_snapshot_bytes=max_path_snapshot,
            audit_max_field_chars=audit_chars,
            backup_root_is_fallback=backup_fallback,
            audit_root_is_fallback=audit_fallback,
        )

    def describe(self) -> str:
        """Human-readable policy summary used by the CLI and diagnostics."""
        roots = "\n".join(f"  - {p}" for p in self.allowed_write_roots)
        notes = []
        if self.backup_root_is_fallback:
            notes.append(
                "  (system backup directory unavailable: run "
                "'sudo bash controller/setup.sh' to use /var/backups/alex)"
            )
        if self.audit_root_is_fallback:
            notes.append(
                "  (system audit directory unavailable: run "
                "'sudo bash controller/setup.sh' to use /var/log/alex-controller)"
            )
        suffix = ("\n" + "\n".join(notes)) if notes else ""
        return (
            f"project:  {self.project_root}\n"
            f"staging:  {self.staging_root}\n"
            f"backups:  {self.backup_root}\n"
            f"audit:    {self.audit_root}\n"
            f"trash:    {self.trash_root}\n"
            f"writable roots:\n{roots or '  (none)'}{suffix}"
        )


_CONFIG: ControllerConfig | None = None


def get_config() -> ControllerConfig:
    """Return the process-wide configuration, creating it on first use."""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = ControllerConfig.from_env()
    return _CONFIG


def set_config(config: ControllerConfig) -> None:
    """Replace the process-wide configuration (used by tests and the CLI)."""
    global _CONFIG
    _CONFIG = config


def reset_config() -> None:
    """Forget the cached configuration."""
    global _CONFIG
    _CONFIG = None
