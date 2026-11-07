"""
error_classifier.py

Classify a tool failure into one of the recovery categories.

Auth and permission errors are never auto-bypassed: they escalate to the
user. Classification is deterministic and uses no LLM, so it is fast and
reliable even when OpenCode is offline.
"""

from __future__ import annotations

import re
from typing import Any

from .recovery_models import ErrorCategory

# Categories that recovery must never auto-bypass; they always escalate to
# the user instead of being repaired silently.
NEVER_AUTO_BYPASS = frozenset({ErrorCategory.AUTH, ErrorCategory.PERMISSION})


# Transient HTTP / network signals that warrant a straight retry.
_TRANSIENT_PATTERNS = (
    r"429",
    r"5\d\d",
    r"timeout",
    r"timed out",
    r"temporarily unavailable",
    r"service unavailable",
    r"connection reset",
    r"connection refused",
    r"network is unreachable",
    r"try again",
)

# Auth signals: never auto-repaired.
_AUTH_PATTERNS = (
    r"401",
    r"403",
    r"unauthorized",
    r"authentication failed",
    r"invalid credentials",
    r"incorrect password",
    r"token.*(expired|invalid|revoked)",
    r"api key.*(invalid|expired)",
    r"login required",
)

# Permission signals: never auto-repaired.
_PERMISSION_PATTERNS = (
    r"permission denied",
    r"forbidden",
    r"access denied",
    r"not allowed",
    r"insufficient.*(permission|privilege|scope)",
    r"guard.*denied",
    r"pathguard",
)

# Environment / dependency signals.
_ENVIRONMENT_PATTERNS = (
    r"no module named",
    r"import.*error",
    r"command not found",
    r"executable.*not found",
    r"dependency",
    r"configuration.*(missing|invalid)",
    r"not configured",
    r"credential.*not found",
)

# Permanent / not-recoverable signals.
_PERMANENT_PATTERNS = (
    r"not found",
    r"no such file",
    r"does not exist",
    r"invalid argument",
    r"unsupported",
    r"not implemented",
)


def _search(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify(error: Any) -> ErrorCategory:
    """Classify an exception or error string into an :class:`ErrorCategory`.

    Never raises. Unknown errors default to :data:`ErrorCategory.PERMANENT`
    so recovery stops and reports rather than guessing.
    """
    text = str(error)

    if _search(_AUTH_PATTERNS, text):
        return ErrorCategory.AUTH
    if _search(_PERMISSION_PATTERNS, text):
        return ErrorCategory.PERMISSION
    if _search(_TRANSIENT_PATTERNS, text):
        return ErrorCategory.TRANSIENT
    if _search(_ENVIRONMENT_PATTERNS, text):
        return ErrorCategory.ENVIRONMENT
    if isinstance(error, (TypeError, ValueError, KeyError)):
        # Likely bad arguments supplied to the tool.
        return ErrorCategory.FIXABLE_ARGS
    if _search(_PERMANENT_PATTERNS, text):
        return ErrorCategory.PERMANENT
    return ErrorCategory.PERMANENT