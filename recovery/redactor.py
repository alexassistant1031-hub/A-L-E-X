"""
redactor.py

Strip secrets and private context from any error report before it leaves
Alex. Reports carry only the error, the tool signature, and minimal context:
never conversation history, memory, or unrelated personal data.
"""

from __future__ import annotations

import re
from typing import Any

# Secret-like patterns redacted from reports, logs, and anything sent to
# OpenCode. Values are replaced with a short labelled token so the report
# stays readable without leaking the secret.
_SECRET_PATTERNS = (
    # Generic bearer / app tokens.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[GITHUB_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_ACCESS_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[GOOGLE_API_KEY]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[SLACK_TOKEN]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[PRIVATE_KEY]"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "[TOKEN]"),
    # Emails (personal data, but kept minimal).
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    # File contents are never included in reports by construction, but catch
    # accidental large quoted blocks.
    (re.compile(r"\b(?:/etc/|~/\.|C:\\\\)[A-Za-z0-9_.\-/\\]+"), "[PATH]"),
)

_KEY_MASK = re.compile(r"(api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*[^\s,;}]+", re.IGNORECASE)


def redact(value: Any) -> Any:
    """Recursively redact secrets from a string, dict, list, or other value.

    Strings are scanned for secret patterns; dicts have their *keys* inspected
    so a field literally named ``password`` is redacted even if the value
    itself doesn't match a token pattern.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            k = str(key)
            if _KEY_MASK.search(k) or k.lower() in (
                "password", "passwd", "pwd", "secret", "api_key", "apikey",
                "token", "access_token", "refresh_token", "private_key",
            ):
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(item)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    return value


def _redact_string(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    text = _KEY_MASK.sub(lambda m: m.group(0).split(":", 1)[0] + ": [REDACTED]", text)
    return text


def redact_report(report: dict[str, Any]) -> dict[str, Any]:
    """Redact a serializable error-report dict before it leaves Alex."""
    return redact(report)