"""
recovery_models.py

Data models for the Central Tool Error Recovery System.

All models are plain dataclasses (no pydantic dependency) so the recovery
system stays import-safe and testable without network or third-party packages.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Tier(Enum):
    """Recovery tiers, applied in order."""

    PLAYBOOK = "tier_0_playbook"        # stored playbook, no LLM
    RULES = "tier_1_rules"             # deterministic rules
    GEMINI_ARGS = "tier_2_gemini_args" # Gemini repairs its own arguments
    OPENCODE = "tier_3_opencode"       # OpenCode for env / code bugs
    GIVE_UP = "tier_4_give_up"         # stop and report to user


class ErrorCategory(Enum):
    """Classification of a tool failure."""

    TRANSIENT = "transient"            # 429 / 5xx / timeout -> retry
    FIXABLE_ARGS = "fixable_args"      # wrong/missing args -> repair + retry
    ENVIRONMENT = "environment"        # missing dependency / config
    CODE_BUG = "code_bug"              # bug in Alex's own tool code
    AUTH = "auth"                      # authentication failure
    PERMISSION = "permission"          # authorization failure
    PERMANENT = "permanent"            # not recoverable automatically


# Categories that recovery must never auto-bypass; they always escalate to
# the user instead of being repaired silently.
NEVER_AUTO_BYPASS = {ErrorCategory.AUTH, ErrorCategory.PERMISSION}


class SafetyTag(Enum):
    """Operation safety tag declared by each write/read tool operation.

    The tag drives the partial-success and confirmation rules applied
    before any recovery retry is executed.
    """

    READ = "read"                      # retry freely
    WRITE_REVERSIBLE = "write_reversible"   # retry allowed after validation
    WRITE_IRREVERSIBLE = "write_irreversible"   # partial-success check required


@dataclass(frozen=True)
class OperationMeta:
    """Static metadata describing a tool operation.

    Attributes:
        tool: Tool group name (e.g. ``email``, ``shell``, ``file_operation``).
        operation: The operation within the tool (e.g. ``send``, ``write``).
        safety_tag: :class:`SafetyTag` declared for this operation.
        error_prefixes: Return-string prefixes that mark a tool result as a
            failure rather than a normal result. Confirmation/approval strings
            are deliberately never listed here.
        verifier: Optional callable ``(result, *args, **kwargs) -> bool``
            that checks whether an irreversible write partially succeeded,
            used to avoid duplicate sends/edits on retry.
    """

    tool: str
    operation: str
    safety_tag: SafetyTag = SafetyTag.READ
    error_prefixes: tuple[str, ...] = ()
    verifier: Any = None


@dataclass(frozen=True)
class ErrorReport:
    """A captured tool failure, ready for the recovery pipeline."""

    error_id: str
    tool: str
    operation: str
    error_type: str
    normalized_message: str
    raw_message: str
    category: ErrorCategory
    args: dict[str, Any]
    fingerprint: str
    created_at: str
    attempt: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        tool: str,
        operation: str,
        error: BaseException | str,
        args: dict[str, Any] | None = None,
        category: ErrorCategory | None = None,
        attempt: int = 0,
    ) -> "ErrorReport":
        from .fingerprint import fingerprint
        from .error_classifier import classify

        raw = str(error)
        normalized = _normalize(raw)
        cat = classify(error) if category is None else category
        return cls(
            error_id=str(uuid.uuid4()),
            tool=tool,
            operation=operation,
            error_type=type(error).__name__ if isinstance(error, BaseException) else "Error",
            normalized_message=normalized,
            raw_message=raw,
            category=cat,
            args=args or {},
            fingerprint=fingerprint(tool, operation, cat, normalized),
            created_at=_utc_now(),
            attempt=attempt,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(message: str) -> str:
    """Light normalization used only for reporting; fingerprint does its own."""
    return message.strip()


@dataclass(frozen=True)
class RecoveryPlan:
    """A proposed recovery action, validated against the strict schema and guard."""

    error_id: str
    diagnosis: str
    category: ErrorCategory
    action: str
    args: dict[str, Any]
    risk_level: str
    requires_confirmation: bool
    retry_allowed: bool
    reason: str
    tier: Tier
    source: str  # "playbook" | "rules" | "gemini" | "opencode"

    VALID_ACTIONS = {
        "retry", "retry_with_args", "install_dependency", "use_fallback",
        "code_patch", "escalate_to_user", "give_up",
    }
    VALID_RISK_LEVELS = {"low", "medium", "high"}

    def is_valid_schema(self) -> bool:
        if self.action not in self.VALID_ACTIONS:
            return False
        if self.risk_level not in self.VALID_RISK_LEVELS:
            return False
        if not isinstance(self.args, dict):
            return False
        return bool(self.error_id and self.diagnosis and self.reason)


@dataclass(frozen=True)
class RecoveryResult:
    """Outcome of the recovery pipeline for one tool call."""

    recovered: bool
    result: str | None = None
    report: ErrorReport | None = None
    plan: RecoveryPlan | None = None
    tier_used: Tier | None = None
    attempts: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    final_message: str = ""
    escalated: bool = False