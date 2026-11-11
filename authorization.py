"""Machine-level authorization policy for Alex's tools.

This is Alex's dedicated machine.  The operator grants full standing
authorization for tool actions, so the model never has to ask for a second
"yes" before sending, posting, writing, running or typing.

When ``ALEX_AUTO_CONFIRM`` is enabled (the default), the tool-execution
boundary sets ``confirmed=True`` and ``authorized=True`` on every call whose
callable accepts those parameters.

This policy does **not** touch hard refusals: privilege escalation, credential
reads, destructive system commands, interpreter pipelines and repository
deletion are blocked inside the tools themselves and are never overridden
here.
"""

from __future__ import annotations

import inspect
import os
from typing import Any, Callable

AUTO_CONFIRM_ENV = "ALEX_AUTO_CONFIRM"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# Parameters that carry the user's authorization into a tool.
_AUTH_FLAGS = ("confirmed", "authorized")


def auto_confirm_enabled() -> bool:
    """Return True when the standing authorization policy is active.

    Defaults to True (the operator's standing authorization).  Set
    ``ALEX_AUTO_CONFIRM=0`` to restore per-call confirmation.
    """
    raw = (os.getenv(AUTO_CONFIRM_ENV) or "1").strip().lower()
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    return True


def apply_auto_authorization(
    func: Callable[..., Any], kwargs: dict[str, Any]
) -> dict[str, Any]:
    """Return ``kwargs`` with authorization flags set, when the policy is on.

    Only parameters the callable actually declares are touched, so tools
    without a confirmation gate are unaffected.
    """
    if not auto_confirm_enabled():
        return kwargs
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return kwargs

    authorized = dict(kwargs)
    for flag in _AUTH_FLAGS:
        if flag in params:
            authorized[flag] = True
    return authorized
