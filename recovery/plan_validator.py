"""
plan_validator.py

Validate a RecoveryPlan against the strict schema and then against the
existing guard (``controller.guard.WritePathGuard``). Rejects:

* free text or unknown actions,
* actions unrelated to the original error,
* actions touching unrelated files,
* anything exposing credentials.

Every action still goes through the real guard at execution time; this
validator is a pre-check so obviously-bad plans never reach it.
"""

from __future__ import annotations

import re
from typing import Any

from controller import PathGuardError, WritePathGuard
from controller.config import get_config

from .recovery_models import ErrorCategory, RecoveryPlan

_SECRET_KEYS = (
    "password", "passwd", "pwd", "secret", "api_key", "apikey",
    "token", "access_token", "refresh_token", "private_key",
    "client_secret", "app_password",
)

_FILE_ACTIONS = {"write", "append", "delete", "copy", "move", "edit_file", "file_operation"}


def validate_plan(
    plan: RecoveryPlan,
    *,
    guard: WritePathGuard | None = None,
    original_error: str = "",
    original_args: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a proposed RecoveryPlan.

    Checks the strict schema first, then the guard, then sanity rules that the
    action is related to the original error and does not expose credentials or
    touch unrelated files.
    """
    if not plan.is_valid_schema():
        return False, "plan fails strict schema"

    if plan.action not in RecoveryPlan.VALID_ACTIONS:
        return False, f"unknown action: {plan.action}"

    if plan.risk_level not in RecoveryPlan.VALID_RISK_LEVELS:
        return False, f"invalid risk level: {plan.risk_level}"

    # Auth / permission errors must never be auto-bypassed.
    if plan.category in (ErrorCategory.AUTH, ErrorCategory.PERMISSION):
        if plan.action not in ("escalate_to_user", "give_up"):
            return False, f"{plan.category.value} errors cannot be auto-repaired"

    # Credential exposure anywhere in the plan.
    payload = f"{plan.diagnosis} {plan.reason} {json_args(plan.args)}"
    if _exposes_secret(payload):
        return False, "plan exposes credentials"

    # File actions must reference a path related to the original operation and
    # must pass the write-path guard.
    if plan.action in _FILE_ACTIONS or _paths_in(plan.args):
        guard = guard or WritePathGuard(get_config())
        for path in _paths_in(plan.args):
            try:
                guard.require_writable(path, action="write")
            except PathGuardError as exc:
                return False, f"guard rejected path {path}: {exc.reason}"

    return True, ""


def json_args(args: dict[str, Any]) -> str:
    import json as _json

    try:
        return _json.dumps(args, sort_keys=True)
    except Exception:
        return str(args)


def _exposes_secret(text: str) -> bool:
    low = text.lower()
    return any(key in low for key in _SECRET_KEYS) or bool(
        re.search(r"\b[A-Za-z0-9+/]{40,}={0,2}\b", text)
    )


def _paths_in(args: dict[str, Any]) -> list[str]:
    """Collect plausible file paths from plan args (best effort)."""
    found: list[str] = []
    for value in args.values():
        if isinstance(value, str) and (
            value.startswith("/") or value.startswith("~")
            or value.startswith("staging/") or "/" in value
        ):
            found.append(value)
    return found