"""Private-directory and atomic-file helpers.

The controller stores backups and audit logs outside the project tree.  Those
directories must be private (0700) and owned by the account running the
controller.  This module never silently falls back to a project-local path:
if the secure location is unusable, callers get a loud error and can refuse
the privileged action.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from .errors import ControllerStorageError

_SETUP_HINT = (
    "Create the controller directories once, owned by the account that runs "
    "Alex, then retry:\n"
    '  sudo install -d -m 0700 -o "$USER" -g "$(id -gn)" '
    "/var/backups/alex /var/log/alex-controller"
)


def _within_owner(uid: int) -> bool:
    """True when ``uid`` is the effective user (root may manage its own dirs)."""
    return uid == os.geteuid()


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` if needed and enforce a private, non-symlink directory.

    Raises:
        ControllerStorageError: missing permission, wrong owner, or not a dir.
    """
    path = Path(path)
    try:
        if path.is_symlink():
            raise ControllerStorageError(
                f"refusing to use symlinked controller directory: {path}"
            )
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ControllerStorageError(
            f"cannot create controller directory {path}: {exc}\n{_SETUP_HINT}"
        ) from exc
    except OSError as exc:
        raise ControllerStorageError(
            f"cannot create controller directory {path}: {exc}"
        ) from exc

    try:
        st = path.lstat()
    except OSError as exc:
        raise ControllerStorageError(f"cannot stat controller directory {path}: {exc}") from exc

    if stat.S_ISLNK(st.st_mode):
        raise ControllerStorageError(f"controller directory is a symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise ControllerStorageError(f"controller path is not a directory: {path}")
    if not _within_owner(st.st_uid):
        raise ControllerStorageError(
            f"controller directory {path} is owned by uid {st.st_uid}, not "
            f"the controller uid {os.geteuid()}. Refusing to use it.\n{_SETUP_HINT}"
        )

    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise ControllerStorageError(
            f"cannot set 0700 on controller directory {path}: {exc}"
        ) from exc
    return path


def secure_open_append(path: Path) -> int:
    """Open a private append-only file without following symlinks.

    Returns a raw file descriptor.  The caller owns it and must close it.
    """
    path = Path(path)
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ControllerStorageError(f"cannot open private log {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ControllerStorageError(f"private log is not a regular file: {path}")
        os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        raise
    return fd


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically and durably.

    The temporary file is created in the same directory so ``os.replace`` is
    atomic on POSIX filesystems.  The parent directory is fsynced afterwards.
    """
    path = Path(path)
    parent = path.parent
    ensure_private_dir(parent)
    tmp = parent / f".{path.name}.tmp-{uuid.uuid4().hex}"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(tmp, flags, mode)
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ControllerStorageError(f"short write to {tmp}")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        _fsync_dir(parent)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """UTF-8 convenience wrapper around :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def fsync_file(path: Path) -> None:
    """Flush a regular file to stable storage."""
    fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync (needed to persist rename/create)."""
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
