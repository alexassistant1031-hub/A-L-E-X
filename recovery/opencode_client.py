"""
opencode_client.py

Talk to the OpenCode Server over HTTP to get a recovery plan for a novel
error. Configurable base URL, timeout, optional auth. Uses a persistent
httpx.Client session (no new process per error). Never raises into the tool
path: on any failure it returns an "unavailable" result so recovery falls
back to Tiers 0-1 and the user is told.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .recovery_models import ErrorCategory, RecoveryPlan, Tier

_UNAVAILABLE = "unavailable"


class OpenCodeClient:
    """Client for the OpenCode recovery endpoint.

    Attributes:
        base_url: Server base URL (no trailing slash).
        timeout: Request timeout in seconds.
        auth: Optional bearer token.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
        auth: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout = timeout
        self.auth = auth
        self._client = httpx.Client(timeout=timeout)

    def get_plan(self, report_json: dict[str, Any]) -> tuple[str, RecoveryPlan | None]:
        """Ask OpenCode for a recovery plan.

        Returns ``(status, plan)`` where status is one of:
            ``"ok"``        - a valid plan was returned
            ``"unavailable"`` - server unreachable / bad response / disabled
            ``"rejected"``  - server returned invalid schema
        Never raises.
        """
        if not self.base_url:
            return _UNAVAILABLE, None
        try:
            headers = {"Content-Type": "application/json"}
            if self.auth:
                headers["Authorization"] = f"Bearer {self.auth}"
            payload = {"report": redact_report(report_json), "schema": "recovery_plan_v1"}
            start = time.time()
            response = self._client.post(
                f"{self.base_url}/recovery/plan",
                json=payload,
                headers=headers,
            )
            elapsed = time.time() - start
            if response.status_code != 200:
                return _UNAVAILABLE, None
            data = response.json()
            plan = _parse_plan(data, report_json.get("error_id", ""))
            if plan is None:
                return "rejected", None
            # Record how long OpenCode took so the manager can budget voice time.
            plan.args.setdefault("_opencode_elapsed", round(elapsed, 3))
            return "ok", plan
        except Exception:
            return _UNAVAILABLE, None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def redact_report(report: dict[str, Any]) -> dict[str, Any]:
    from .redactor import redact

    return redact(report)


def _parse_plan(data: dict[str, Any], error_id: str) -> RecoveryPlan | None:
    """Parse OpenCode JSON into a RecoveryPlan, or None if it violates the schema."""
    from .recovery_models import ErrorCategory, Tier

    if not isinstance(data, dict):
        return None
    try:
        category = ErrorCategory(data.get("category", "permanent"))
        action = data.get("action", "give_up")
        tier = Tier(data.get("tier", "tier_3_opencode"))
        return RecoveryPlan(
            error_id=str(data.get("error_id", error_id)),
            diagnosis=str(data.get("diagnosis", ""))[:500],
            category=category,
            action=str(action),
            args=data.get("args", {}) or {},
            risk_level=str(data.get("risk_level", "low")),
            requires_confirmation=bool(data.get("requires_confirmation", True)),
            retry_allowed=bool(data.get("retry_allowed", True)),
            reason=str(data.get("reason", ""))[:500],
            tier=tier,
            source="opencode",
        )
    except Exception:
        return None