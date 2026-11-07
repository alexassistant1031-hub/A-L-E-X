"""
fingerprint.py

Stable fingerprints for tool failures so the same class of error can be
looked up in the playbook store without calling an LLM.

The fingerprint is ``sha256(tool + operation + error_type + normalized_message)``
where normalization strips paths, ids, timestamps, numbers and tokens so that
environment-specific details don't split one error class into many.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


# Placeholders used during normalization, kept stable so fingerprints are
# reproducible across machines and runs.
_PATH = "<path>"
_ID = "<id>"
_TIME = "<time>"
_NUM = "<num>"
_TOKEN = "<token>"

_PATH_RE = re.compile(r"/[A-Za-z0-9_.\-]+")
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][A-Za-z0-9_.\- \\/]*|~[/\\][A-Za-z0-9_.\- \\/]*")
_ID_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
_TIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}.*\b")
_NUM_RE = re.compile(r"\b\d+\b")
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _normalize_message(message: str) -> str:
    """Strip variable details from an error message.

    Paths, ids, timestamps, bare numbers and long tokens are replaced with
    stable placeholders so the same error class yields the same fingerprint.
    """
    text = message
    text = _ABS_PATH_RE.sub(_PATH, text)
    text = _PATH_RE.sub(_PATH, text)
    text = _TIME_RE.sub(_TIME, text)
    text = _ID_RE.sub(_ID, text)
    text = _TOKEN_RE.sub(_TOKEN, text)
    text = _NUM_RE.sub(_NUM, text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def fingerprint(
    tool: str,
    operation: str,
    error_type_or_category: str | Any,
    message: str,
) -> str:
    """Return a stable sha256 fingerprint for a tool failure.

    Args:
        tool: Tool group name (e.g. ``email``).
        operation: Operation within the tool (e.g. ``send``).
        error_type_or_category: Exception type name or ErrorCategory.
        message: Raw error message; normalized internally.
    """
    etype = (
        error_type_or_category.value
        if hasattr(error_type_or_category, "value")
        else str(error_type_or_category)
    )
    canonical = "|".join(
        [
            tool.strip().lower(),
            operation.strip().lower(),
            etype.strip().lower(),
            _normalize_message(message),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()