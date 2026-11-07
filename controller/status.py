"""Read-only status reporting for the controller.

``python -m controller status`` and tests use this module to answer two
questions without changing anything on disk:

* are the private backup/audit directories usable?
* which paths is the agent allowed to write?

The functions are intentionally side-effect free: they never create a
directory and never write an audit record.  Use ``python -m controller init``
to create missing storage.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .config import ControllerConfig, get_config


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            break
        probe = probe.parent
    return probe


def _inspect_private_dir(path: Path) -> dict[str, Any]:
    """Inspect a controller directory without creating it."""
    result: dict[str, Any] = {"path": str(path), "ok": False, "message": ""}
    try:
        if path.is_symlink():
            result["message"] = "is a symlink; refusing to use it"
            return result
        if path.exists():
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode):
                result["message"] = "exists but is not a directory"
                return result
            if st.st_uid != os.geteuid():
                result["message"] = (
                    f"owned by uid {st.st_uid}, expected {os.geteuid()}"
                )
                return result
            result["mode"] = oct(stat.S_IMODE(st.st_mode))
            if not os.access(path, os.W_OK | os.X_OK):
                result["message"] = "not writable by the controller account"
                return result
            result["ok"] = True
            result["message"] = "ready"
            return result

        ancestor = _nearest_existing(path)
        if ancestor.is_symlink() or not ancestor.is_dir():
            result["message"] = f"nearest existing ancestor is not a directory: {ancestor}"
            return result
        if not os.access(ancestor, os.W_OK | os.X_OK):
            result["message"] = f"nearest existing ancestor is not writable: {ancestor}"
            return result
        result["ok"] = True
        result["message"] = "will be created on first use"
        return result
    except OSError as exc:
        result["message"] = f"cannot inspect: {exc}"
        return result


def controller_status(config: ControllerConfig | None = None) -> dict[str, Any]:
    """Return a JSON-serialisable snapshot of controller readiness and policy."""
    cfg = config or get_config()
    checks = {
        "backup": _inspect_private_dir(cfg.backup_root),
        "audit": _inspect_private_dir(cfg.audit_root),
        "staging": _inspect_private_dir(cfg.staging_root),
    }
    ready = all(item["ok"] for item in checks.values())
    failures = [
        f"{name}: {item['message']}"
        for name, item in checks.items()
        if not item["ok"]
    ]
    return {
        "ready": ready,
        "message": "ready" if ready else "; ".join(failures),
        "project_root": str(cfg.project_root),
        "staging_root": str(cfg.staging_root),
        "backup_root": str(cfg.backup_root),
        "audit_root": str(cfg.audit_root),
        "trash_root": str(cfg.trash_root),
        "backup_fallback": cfg.backup_root_is_fallback,
        "audit_fallback": cfg.audit_root_is_fallback,
        "allowed_write_roots": [str(path) for path in cfg.allowed_write_roots],
        "checks": checks,
    }


def describe_status(status: dict[str, Any] | None = None) -> str:
    """Render :func:`controller_status` as human-readable text."""
    data = status or controller_status()
    lines = [
        f"ready:    {'yes' if data['ready'] else 'NO'} — {data['message']}",
        f"project:  {data['project_root']}",
        f"staging:  {data['staging_root']}",
        f"backups:  {data['backup_root']}",
        f"audit:    {data['audit_root']}",
        f"trash:    {data['trash_root']}",
    ]
    if data.get("backup_fallback"):
        lines.append(
            "note:     /var/backups/alex is unavailable; using the private "
            "XDG fallback. Run 'sudo bash controller/setup.sh' to fix."
        )
    if data.get("audit_fallback"):
        lines.append(
            "note:     /var/log/alex-controller is unavailable; using the "
            "private XDG fallback. Run 'sudo bash controller/setup.sh' to fix."
        )
    lines.append("writable roots:")
    roots = data.get("allowed_write_roots") or []
    if roots:
        lines.extend(f"  - {root}" for root in roots)
    else:
        lines.append("  (none)")
    return "\n".join(lines)


__all__ = ["controller_status", "describe_status"]
