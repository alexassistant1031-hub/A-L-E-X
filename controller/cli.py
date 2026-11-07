"""Command-line interface for the self-modification controller.

Examples::

    python -m controller status
    python -m controller backup --reason "before prompt edit"
    python -m controller list
    python -m controller verify
    python -m controller restore alex-... /tmp/alex-restored --confirm RESTORE
    python -m controller audit-verify --stream file
    python -m controller audit-tail --stream file --limit 10

The CLI only uses the controller's own storage; it never invokes Git.
"""

from __future__ import annotations

import argparse
import json
import sys

from .audit import get_audit
from .backup import BackupManager, BackupRecord
from .config import get_config
from .errors import ControllerError
from .status import controller_status, describe_status
from .storage import ensure_private_dir


def _format_record(record: BackupRecord) -> str:
    return (
        f"{record.name}\n"
        f"  created: {record.created_at}\n"
        f"  reason:  {record.reason} ({record.trigger})\n"
        f"  files:   {record.file_count}  bytes: {record.total_bytes}\n"
        f"  sha256:  {record.sha256}\n"
        f"  archive: {record.archive}"
    )


def _find_path_record(manager: BackupManager, name: str) -> BackupRecord:
    for record in manager.list_path_snapshots():
        if name in (record.name, record.archive.name, str(record.archive)):
            return record
    raise ControllerError(f"unknown path snapshot: {name}")


def _cmd_status(_args: argparse.Namespace) -> int:
    cfg = get_config()
    status = controller_status(cfg)
    print(cfg.describe())
    print()
    print(describe_status(status))
    return 0 if status["ready"] else 2


def _cmd_init(_args: argparse.Namespace) -> int:
    cfg = get_config()
    for label, path in (
        ("staging", cfg.staging_root),
        ("backup", cfg.backup_root),
        ("audit", cfg.audit_root),
    ):
        ensure_private_dir(path)
        print(f"{label}: {path}")
    return _cmd_status(_args)


def _cmd_backup(args: argparse.Namespace) -> int:
    manager = BackupManager(get_config())
    record = manager.create(args.reason, trigger="manual")
    print(_format_record(record))
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    manager = BackupManager(get_config())
    project = manager.list_backups()
    paths = manager.list_path_snapshots()
    print(f"project snapshots ({len(project)}):")
    for record in project:
        print(f"  {record.created_at}  {record.name}  ({record.file_count} files)")
    print(f"path snapshots ({len(paths)}):")
    for record in paths:
        print(f"  {record.created_at}  {record.name}  ({record.reason})")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    manager = BackupManager(get_config())
    targets: list[BackupRecord]
    if args.target:
        targets = [manager._record_for(args.target)]  # noqa: SLF001 - CLI helper
    else:
        targets = manager.list_backups() + manager.list_path_snapshots()

    failed = 0
    for record in targets:
        result = manager.verify(record)
        marker = "OK  " if result.ok else "FAIL"
        print(f"{marker} {record.name}: {result.message}")
        if not result.ok:
            failed += 1
    if not targets:
        print("no snapshots to verify")
    return 1 if failed else 0


def _cmd_prune(args: argparse.Namespace) -> int:
    manager = BackupManager(get_config())
    removed = manager.prune(keep=args.keep)
    for name in removed:
        print(f"removed {name}")
    if not removed:
        print("nothing to prune")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    manager = BackupManager(get_config())
    record = manager._record_for(args.name)  # noqa: SLF001 - CLI helper
    destination = manager.restore(
        record, args.destination, confirm=args.confirm, overwrite=args.overwrite
    )
    print(f"restored {record.name} -> {destination}")
    return 0


def _cmd_restore_path(args: argparse.Namespace) -> int:
    manager = BackupManager(get_config())
    record = _find_path_record(manager, args.name)
    destination = manager.restore(
        record, args.destination, confirm=args.confirm, overwrite=args.overwrite
    )
    print(f"restored {record.name} -> {destination}")
    return 0


def _cmd_audit_verify(args: argparse.Namespace) -> int:
    log = get_audit(args.stream)
    ok, count, message = log.verify()
    print(f"{'OK' if ok else 'FAIL'} {args.stream}: {count} records — {message}")
    return 0 if ok else 1


def _cmd_audit_tail(args: argparse.Namespace) -> int:
    log = get_audit(args.stream)
    for record in log.tail(args.limit):
        print(json.dumps(record, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m controller",
        description="Inspect and operate Alex's self-modification controller.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show storage policy and readiness")
    sub.add_parser("init", help="create private directories and staging")
    sub.add_parser("list", help="list project and path snapshots")

    backup = sub.add_parser("backup", help="create a project snapshot now")
    backup.add_argument("--reason", default="manual", help="why the snapshot exists")

    verify = sub.add_parser("verify", help="verify checksums and archives")
    verify.add_argument("target", nargs="?", help="snapshot name or archive path")

    prune = sub.add_parser("prune", help="delete old project snapshots")
    prune.add_argument("--keep", type=int, default=None, help="how many to keep")

    restore = sub.add_parser("restore", help="restore a project snapshot")
    restore.add_argument("name", help="snapshot name or archive path")
    restore.add_argument("destination", help="empty or non-existent directory")
    restore.add_argument("--confirm", required=True, help='must be exactly "RESTORE"')
    restore.add_argument("--overwrite", action="store_true", help="replace a non-empty destination")

    restore_path = sub.add_parser("restore-path", help="restore a single-path snapshot")
    restore_path.add_argument("name", help="snapshot name or archive path")
    restore_path.add_argument("destination", help="empty or non-existent directory")
    restore_path.add_argument("--confirm", required=True, help='must be exactly "RESTORE"')
    restore_path.add_argument("--overwrite", action="store_true", help="replace a non-empty destination")

    audit_verify = sub.add_parser("audit-verify", help="verify the hash chain")
    audit_verify.add_argument("--stream", default="controller", help="file, typing, backup, controller")

    audit_tail = sub.add_parser("audit-tail", help="print recent audit records")
    audit_tail.add_argument("--stream", default="controller")
    audit_tail.add_argument("--limit", type=int, default=20)

    return parser


_COMMANDS = {
    "status": _cmd_status,
    "init": _cmd_init,
    "backup": _cmd_backup,
    "list": _cmd_list,
    "verify": _cmd_verify,
    "prune": _cmd_prune,
    "restore": _cmd_restore,
    "restore-path": _cmd_restore_path,
    "audit-verify": _cmd_audit_verify,
    "audit-tail": _cmd_audit_tail,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"
    args.command = command
    try:
        return _COMMANDS[command](args)
    except (ControllerError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
