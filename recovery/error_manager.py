"""
error_manager.py

Central coordinator for the recovery pipeline. Runs the tier pipeline
(Tier 0 playbook -> Tier 1 rules -> Tier 2 Gemini args -> Tier 3 OpenCode ->
Tier 4 give up), tracks attempts, writes audit logs, and returns the final
result.

Every proposed action passes through plan_validator and the existing guard
before executing; recovery can never turn a DENY into an ALLOW or skip an ASK.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from controller import get_audit

from .circuit_breaker import CircuitBreaker
from .error_classifier import ErrorCategory, NEVER_AUTO_BYPASS, classify
from .fingerprint import fingerprint
from .opencode_client import OpenCodeClient
from .plan_validator import validate_plan
from .playbook_store import PlaybookStore
from .redactor import redact_report
from .recovery_models import (
    ErrorReport,
    RecoveryPlan,
    RecoveryResult,
    SafetyTag,
    Tier,
    _utc_now,
)
from .retry_manager import RetryManager

# Actions that genuinely change state and need confirmation when not
# explicitly authorized by the user.
_CONFIRM_ACTIONS = {
    "install_dependency", "code_patch", "retry_with_args",
}

_AUDIT_STREAM = "recovery"


class ErrorManager:
    """Coordinate recovery for a single tool call."""

    def __init__(
        self,
        *,
        store: PlaybookStore | None = None,
        breaker: CircuitBreaker | None = None,
        retry: RetryManager | None = None,
        opencode: OpenCodeClient | None = None,
        audit_stream: str = _AUDIT_STREAM,
        enabled: bool = True,
        max_auto_attempts: int = 3,
        backoff_base: float = 1.0,
        circuit_threshold: int = 5,
        circuit_cooldown: float = 300.0,
        gemini_repair: Callable[[ErrorReport, dict[str, Any]], RecoveryPlan | None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.max_auto_attempts = max_auto_attempts
        self.backoff_base = backoff_base
        self.store = store or PlaybookStore()
        self.breaker = breaker or CircuitBreaker(
            threshold=circuit_threshold, cooldown=circuit_cooldown
        )
        self.opencode = opencode or OpenCodeClient()
        self.gemini_repair = gemini_repair
        self.audit_stream = audit_stream
        self._retry_factories: dict[str, RetryManager] = {}

    # ── public entry point ─────────────────────────────────────────────

    def handle(
        self,
        report: ErrorReport,
        *,
        retry: Callable[[dict[str, Any]], Any],
        meta,
    ) -> RecoveryResult:
        """Run the recovery pipeline for ``report``.

        ``retry`` re-invokes the tool with a (possibly corrected) argument dict
        and returns the tool's result or raises on failure. ``meta`` is the
        :class:`OperationMeta` describing the operation.
        """
        return asyncio.run(self.handle_async(report, retry=retry, meta=meta))

    async def handle_async(
        self,
        report: ErrorReport,
        *,
        retry: Callable[[dict[str, Any]], Any],
        meta,
    ) -> RecoveryResult:
        """Async core of the recovery pipeline (awaits the async retry)."""
        history: list[dict[str, Any]] = []
        attempt = 0
        retry_mgr = RetryManager(max_attempts=self.max_auto_attempts, backoff_base=self.backoff_base)
        current_args = dict(report.args)

        # Circuit breaker: pause auto-recovery for this tool while open.
        if self.breaker.is_open(meta.tool):
            self._audit("circuit_open", tool=meta.tool, error_id=report.error_id)
            return RecoveryResult(
                recovered=False,
                report=report,
                tier_used=Tier.GIVE_UP,
                attempts=attempt,
                history=history,
                escalated=True,
                final_message=(
                    f"Auto-recovery for {meta.tool} is paused (circuit breaker "
                    f"open). Try again in {self.breaker.remaining_cooldown(meta.tool):.0f}s "
                    "or retry manually."
                ),
            )

        # If the category must never be auto-bypassed, escalate straight away.
        if report.category in NEVER_AUTO_BYPASS:
            self._audit(
                "escalate_no_auto",
                tool=meta.tool,
                error_id=report.error_id,
                category=report.category.value,
            )
            return RecoveryResult(
                recovered=False,
                report=report,
                tier_used=Tier.GIVE_UP,
                attempts=attempt,
                history=history,
                escalated=True,
                final_message=(
                    f"{report.category.value} error on {meta.tool}.{meta.operation}: "
                    f"{report.normalized_message}. Escalating to you."
                ),
            )
        # ── tier pipeline ────────────────────────────────────────────
        for tier in (Tier.PLAYBOOK, Tier.RULES, Tier.GEMINI_ARGS, Tier.OPENCODE):
            attempt += 1
            retry_mgr.record(report.fingerprint, report.category, "tier_started")

            plan = self._plan_for_tier(tier, report, current_args, meta)
            if plan is None:
                history.append({"tier": tier.value, "outcome": "no_plan"})
                continue

            # Validate plan against schema + guard.
            ok, reason = validate_plan(
                plan,
                original_error=report.raw_message,
                original_args=report.args,
            )
            if not ok:
                self._audit(
                    "plan_rejected",
                    tool=meta.tool,
                    error_id=report.error_id,
                    tier=tier.value,
                    reason=reason,
                )
                history.append({"tier": tier.value, "outcome": "rejected", "reason": reason})
                if report.category in NEVER_AUTO_BYPASS:
                    break
                continue

            # Confirmation gate: actions needing user confirmation.
            if plan.requires_confirmation and plan.action in _CONFIRM_ACTIONS:
                self._audit(
                    "confirmation_required",
                    tool=meta.tool,
                    error_id=report.error_id,
                    tier=tier.value,
                    action=plan.action,
                )
                history.append(
                    {"tier": tier.value, "outcome": "confirmation_required", "action": plan.action}
                )
                return RecoveryResult(
                    recovered=False,
                    report=report,
                    plan=plan,
                    tier_used=tier,
                    attempts=attempt,
                    history=history,
                    escalated=True,
                    final_message=(
                        f"Recovery for {meta.tool} needs your confirmation: {plan.reason}. "
                        "Say yes to proceed."
                    ),
                )

            # Execute the approved plan.
            outcome = await self._execute_plan(plan, retry, current_args, meta, report, history)
            if outcome.get("recovered"):
                # Verified success: store playbook (Tier 0 only), reset circuit.
                if tier == Tier.PLAYBOOK:
                    self.store.record_success(report.fingerprint, plan)
                self.breaker.record_success(meta.tool)
                self._audit(
                    "recovered",
                    tool=meta.tool,
                    error_id=report.error_id,
                    tier=tier.value,
                    action=plan.action,
                    attempts=attempt,
                )
                return RecoveryResult(
                    recovered=True,
                    result=outcome["result"],
                    report=report,
                    plan=plan,
                    tier_used=tier,
                    attempts=attempt,
                    history=history,
                    final_message=outcome.get("message", ""),
                )

            # Plan failed: record and try next tier.
            retry_mgr.record(report.fingerprint, report.category, "tier_failed")
            self.store.record_fail(report.fingerprint)
            history.append(
                {"tier": tier.value, "outcome": "failed", "error": outcome.get("error", "")}
            )
            stop, stop_reason = retry_mgr.should_stop(report.category, report.fingerprint)
            if stop:
                self._audit(
                    "stop",
                    tool=meta.tool,
                    error_id=report.error_id,
                    tier=tier.value,
                    reason=stop_reason,
                    attempts=attempt,
                )
                self.breaker.record_failure(meta.tool)
                return RecoveryResult(
                    recovered=False,
                    report=report,
                    plan=plan,
                    tier_used=tier,
                    attempts=attempt,
                    history=history,
                    escalated=True,
                    final_message=(
                        f"Recovery exhausted for {meta.tool}.{meta.operation}: {stop_reason}. "
                        f"Original: {report.normalized_message}. Latest: "
                        f"{outcome.get('error', report.normalized_message)}."
                    ),
                )

            # Backoff before next tier.
            delay = retry_mgr.backoff_seconds()
            if delay:
                await asyncio.sleep(min(delay, 5.0))

        # ── Tier 4: give up ──────────────────────────────────────────
        self.breaker.record_failure(meta.tool)
        self._audit(
            "give_up",
            tool=meta.tool,
            error_id=report.error_id,
            tier=Tier.GIVE_UP.value,
            attempts=attempt,
        )
        return RecoveryResult(
            recovered=False,
            report=report,
            tier_used=Tier.GIVE_UP,
            attempts=attempt,
            history=history,
            escalated=True,
            final_message=(
                f"Could not recover {meta.tool}.{meta.operation} automatically. "
                f"Original: {report.normalized_message}."
            ),
        )

    # ── tier plan builders ───────────────────────────────────────────

    def _plan_for_tier(
        self, tier: Tier, report: ErrorReport, args: dict[str, Any], meta
    ) -> RecoveryPlan | None:
        if tier == Tier.PLAYBOOK:
            return self._tier_playbook(report)
        if tier == Tier.RULES:
            return self._tier_rules(report, args, meta)
        if tier == Tier.GEMINI_ARGS:
            return self._tier_gemini(report, args)
        if tier == Tier.OPENCODE:
            return self._tier_opencode(report, args)
        return None

    def _tier_playbook(self, report: ErrorReport) -> RecoveryPlan | None:
        plan = self.store.lookup(report.fingerprint)
        if plan is None:
            return None
        return RecoveryPlan(
            error_id=report.error_id,
            diagnosis="Stored playbook for known error.",
            category=report.category,
            action=plan.action,
            args=plan.args,
            risk_level="low",
            requires_confirmation=False,
            retry_allowed=True,
            reason="Known fix from a previously verified retry.",
            tier=Tier.PLAYBOOK,
            source="playbook",
        )

    def _tier_rules(self, report: ErrorReport, args: dict[str, Any], meta) -> RecoveryPlan | None:
        cat = report.category
        text = report.normalized_message

        if cat == ErrorCategory.TRANSIENT:
            return RecoveryPlan(
                error_id=report.error_id,
                diagnosis="Transient error; retry with backoff.",
                category=cat,
                action="retry",
                args=args,
                risk_level="low",
                requires_confirmation=False,
                retry_allowed=True,
                reason="Transient failures often clear on retry.",
                tier=Tier.RULES,
                source="rules",
            )

        if cat == ErrorCategory.FIXABLE_ARGS:
            # Simple deterministic argument fixes are applied here.
            fixed = self._simple_arg_fixes(text, args, meta)
            if fixed is not None:
                return RecoveryPlan(
                    error_id=report.error_id,
                    diagnosis="Simple argument fix applied.",
                    category=cat,
                    action="retry_with_args",
                    args=fixed,
                    risk_level="low",
                    requires_confirmation=False,
                    retry_allowed=True,
                    reason="Deterministic argument correction.",
                    tier=Tier.RULES,
                    source="rules",
                )
        return None

    def _tier_gemini(self, report: ErrorReport, args: dict[str, Any]) -> RecoveryPlan | None:
        if self.gemini_repair is None:
            return None
        try:
            return self.gemini_repair(report, args)
        except Exception:
            return None

    def _tier_opencode(self, report: ErrorReport, args: dict[str, Any]) -> RecoveryPlan | None:
        status, plan = self.opencode.get_plan(_report_dict(report, args))
        if status != "ok" or plan is None:
            return None
        plan.error_id = report.error_id
        return plan

    # ── plan execution ───────────────────────────────────────────────

    async def _execute_plan(
        self,
        plan: RecoveryPlan,
        retry: Callable[[dict[str, Any]], Any],
        current_args: dict[str, Any],
        meta,
        report: ErrorReport,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Execute an approved plan through the guard. Returns a dict with
        ``recovered``, ``result`` and/or ``error``."""
        action = plan.action
        args = dict(plan.args)

        if action == "retry":
            return await self._attempt(retry, args, meta, report, history)

        if action == "retry_with_args":
            current_args.update(args)
            return await self._attempt(retry, current_args, meta, report, history)

        if action == "use_fallback":
            # Fallback is a retry with the same args (tool decides).
            return await self._attempt(retry, current_args, meta, report, history)

        if action == "install_dependency":
            return {
                "recovered": False,
                "error": "install_dependency requires user confirmation and is not auto-executed",
            }

        if action == "code_patch":
            return {
                "recovered": False,
                "error": "code_patch is staged for user approval, not auto-applied",
            }

        if action in ("escalate_to_user", "give_up"):
            return {"recovered": False, "error": plan.reason}

        return {"recovered": False, "error": f"unsupported action: {action}"}

    async def _attempt(
        self,
        retry: Callable[[dict[str, Any]], Any],
        args: dict[str, Any],
        meta,
        report: ErrorReport,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            result = await retry(args)
        except Exception as exc:
            return {"recovered": False, "error": str(exc)}
        # Treat explicit error-string returns as failures.
        if isinstance(result, str) and result.startswith(meta.error_prefixes):
            return {"recovered": False, "error": result}
        # Partial-success check for irreversible writes.
        if (
            meta.safety_tag == SafetyTag.WRITE_IRREVERSIBLE
            and meta.verifier is not None
        ):
            try:
                if not meta.verifier(result, args):
                    return {
                        "recovered": False,
                        "error": "partial success could not be verified; not retrying to avoid duplicates",
                    }
            except Exception:
                return {
                    "recovered": False,
                    "error": "verifier raised; not retrying irreversible write",
                }
        return {"recovered": True, "result": result}

    # ── helpers ──────────────────────────────────────────────────────

    def _simple_arg_fixes(
        self, text: str, args: dict[str, Any], meta
    ) -> dict[str, Any] | None:
        """Deterministic, safe argument corrections. Returns fixed args or None."""
        # Placeholder: wiring can add real fixes here. Keep conservative so no
        # incorrect args are ever submitted.
        return None

    def _audit(self, event: str, **fields: Any) -> None:
        """Write one redacted audit record through the existing audit infra."""
        try:
            log = get_audit(self.audit_stream)
            log.write(event, **redact_report(fields))
        except Exception:
            # Audit failures must not break the tool path.
            pass


def _report_dict(report: ErrorReport, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "error_id": report.error_id,
        "tool": report.tool,
        "operation": report.operation,
        "error_type": report.error_type,
        "normalized_message": report.normalized_message,
        "args": args,
        "category": report.category.value,
        "fingerprint": report.fingerprint,
        "attempt": report.attempt,
    }


def run_recovery(
    *,
    tool: str,
    operation: str,
    error: BaseException | str,
    args: dict[str, Any],
    meta,
    retry: Callable[[dict[str, Any]], Any],
    manager: ErrorManager,
) -> RecoveryResult:
    """Convenience: build a report and run the manager."""
    report = ErrorReport.new(
        tool=tool,
        operation=operation,
        error=error,
        args=args,
    )
    return manager.handle(report, retry=retry, meta=meta)
