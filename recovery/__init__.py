"""Central Tool Error Recovery System for Alex.

Recoverable tool failures are handled automatically at the tool-execution
boundary (``recovery.executor``) through a tiered pipeline:

    Tier 0: stored playbook (SQLite, no LLM)
    Tier 1: deterministic rules (backoff, path normalization, arg fixes)
    Tier 2: Gemini repairs its own tool arguments
    Tier 3: OpenCode for environment / code-bug problems
    Tier 4: stop and report to the user with full history

Recovery is additive: if it is unavailable or throws, tools behave exactly
as they do today. Every proposed action passes through the existing guard
and confirmation flow before executing; recovery can never turn a DENY into
an ALLOW or skip an ASK.
"""

from __future__ import annotations

from .recovery_models import (
    ErrorCategory,
    ErrorReport,
    OperationMeta,
    RecoveryPlan,
    RecoveryResult,
    SafetyTag,
    Tier,
)
from .error_manager import ErrorManager, run_recovery
from .executor import execute_tool

__all__ = [
    "ErrorCategory",
    "ErrorReport",
    "OperationMeta",
    "RecoveryPlan",
    "RecoveryResult",
    "SafetyTag",
    "Tier",
    "ErrorManager",
    "run_recovery",
    "execute_tool",
]