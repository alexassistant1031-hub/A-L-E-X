"""
executor.py

The single tool-execution boundary. Wraps every tool call:

    result = await execute_tool(tool, operation, safety_tag, func, *args,
                                error_prefixes=(), verifier=None, **kwargs)

On success, the result is returned unchanged (recovery is never invoked).
On failure (raised exception, or a result string matching ``error_prefixes``),
an ``ErrorReport`` is built and handed to the ``ErrorManager`` tier pipeline.

Recovery is additive: if it is disabled, throws, or is otherwise unavailable,
the original tool behaviour is preserved exactly.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .error_manager import ErrorManager, run_recovery
from .recovery_models import OperationMeta, SafetyTag


# Global manager, set by main.py at startup. Until then recovery is disabled
# and tools pass straight through.
_manager: ErrorManager | None = None


def configure(manager: ErrorManager) -> None:
    """Install the shared :class:`ErrorManager` (called once from main.py)."""
    global _manager
    _manager = manager


async def execute_tool(
    *,
    tool: str,
    operation: str,
    safety_tag: SafetyTag = SafetyTag.READ,
    func: Callable[..., Any],
    error_prefixes: tuple[str, ...] = (),
    verifier: Any = None,
    **kwargs: Any,
) -> Any:
    """Execute ``func(**kwargs)`` with automatic recovery on failure.

    Returns the tool result on success or after successful recovery, or a
    user-facing final message when recovery is exhausted.
    """
    meta = OperationMeta(
        tool=tool,
        operation=operation,
        safety_tag=safety_tag,
        error_prefixes=error_prefixes,
        verifier=verifier,
    )

    async def attempt(args: dict[str, Any]) -> Any:
        return await func(**_accepted_kwargs(func, args or kwargs))

    try:
        result = await attempt({})
    except Exception as exc:
        return await _handle_failure(exc, kwargs, meta, attempt)

    # A tool may return an explicit error string instead of raising. Only
    # treat it as a failure if it matches one of the declared prefixes;
    # confirmation/approval strings are never treated as failures.
    if isinstance(result, str) and error_prefixes and result.startswith(error_prefixes):
        class _StrError(Exception):
            pass

        return await _handle_failure(_StrError(result), kwargs, meta, attempt)

    return result


def _accepted_kwargs(func: Callable[..., Any], args: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the target callable does not accept.

    A recovery plan may carry extra keys (for example a timing marker added by
    the client).  Passing those through would raise ``TypeError`` and mask the
    real failure, so filter against the signature unless it takes ``**kwargs``.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return args
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args
    return {key: value for key, value in args.items() if key in params}


async def _handle_failure(
    exc: BaseException,
    kwargs: dict[str, Any],
    meta: OperationMeta,
    attempt: Callable[[dict[str, Any]], Any],
) -> Any:
    # If recovery is disabled or not configured, surface the original error.
    if _manager is None or not _manager.enabled:
        return f"{meta.tool}: {exc}"

    from .error_classifier import classify
    from .fingerprint import fingerprint

    from .recovery_models import ErrorReport

    report = ErrorReport.new(
        tool=meta.tool,
        operation=meta.operation,
        error=exc,
        args=kwargs,
        category=classify(exc),
    )

    async def retry(args: dict[str, Any]) -> Any:
        return await attempt(args)

    result = await _manager.handle_async(report, retry=retry, meta=meta)
    if result.recovered:
        return result.result
    return result.final_message or f"{meta.tool}: {exc}"