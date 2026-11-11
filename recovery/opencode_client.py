"""
opencode_client.py

Talk to the OpenCode Server over HTTP to get a recovery plan for a novel
error. Configurable base URL, timeout, optional auth. Uses a persistent
httpx client session (no new process per error). Never raises into the tool
path: on any failure it returns an "unavailable" result so recovery falls
back to Tiers 0-1 and the user is told.

Two entry points are provided:

* ``get_plan`` - synchronous, used by scripts and legacy callers.
* ``get_plan_async`` - used by the recovery pipeline.  It never blocks the
  LiveKit event loop, so a slow or unreachable OpenCode server cannot stall
  the voice session.
"""

from __future__ import annotations

from typing import Any

import httpx

from .recovery_models import RecoveryPlan, Tier

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
        self._aclient: httpx.AsyncClient | None = None

    # ── request building ───────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth}"
        return headers

    def _payload(self, report_json: dict[str, Any]) -> dict[str, Any]:
        return {
            "report": redact_report(report_json),
            "schema": "recovery_plan_v1",
        }

    def _url(self) -> str:
        return f"{self.base_url}/recovery/plan"

    # ── public API ─────────────────────────────────────────────────────

    def get_plan(self, report_json: dict[str, Any]) -> tuple[str, RecoveryPlan | None]:
        """Ask OpenCode for a recovery plan (blocking).

        Returns ``(status, plan)`` where status is one of:
            ``"ok"``        - a valid plan was returned
            ``"unavailable"`` - server unreachable / bad response / disabled
            ``"rejected"``  - server returned invalid schema
        Never raises.
        """
        if not self.base_url:
            return _UNAVAILABLE, None
        try:
            response = self._client.post(
                self._url(),
                json=self._payload(report_json),
                headers=self._headers(),
            )
            return _interpret(response, report_json.get("error_id", ""))
        except Exception:
            return _UNAVAILABLE, None

    async def get_plan_async(
        self, report_json: dict[str, Any]
    ) -> tuple[str, RecoveryPlan | None]:
        """Async variant for the voice path; never blocks the event loop."""
        if not self.base_url:
            return _UNAVAILABLE, None
        try:
            if self._aclient is None:
                self._aclient = httpx.AsyncClient(timeout=self.timeout)
            response = await self._aclient.post(
                self._url(),
                json=self._payload(report_json),
                headers=self._headers(),
            )
            return _interpret(response, report_json.get("error_id", ""))
        except Exception:
            return _UNAVAILABLE, None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    async def aclose(self) -> None:
        """Close both the sync and async sessions."""
        self.close()
        if self._aclient is not None:
            try:
                await self._aclient.aclose()
            except Exception:
                pass
            finally:
                self._aclient = None


def redact_report(report: dict[str, Any]) -> dict[str, Any]:
    from .redactor import redact

    return redact(report)


def _interpret(
    response: httpx.Response, error_id: str
) -> tuple[str, RecoveryPlan | None]:
    """Turn an HTTP response into a ``(status, plan)`` pair. Never raises."""
    if response.status_code != 200:
        return _UNAVAILABLE, None
    try:
        data = response.json()
    except Exception:
        return "rejected", None
    plan = _parse_plan(data, error_id)
    if plan is None:
        return "rejected", None
    return "ok", plan


def _parse_plan(data: dict[str, Any], error_id: str) -> RecoveryPlan | None:
    """Parse OpenCode JSON into a RecoveryPlan, or None if it violates the schema."""
    from .recovery_models import ErrorCategory

    if not isinstance(data, dict):
        return None
    try:
        category = ErrorCategory(data.get("category", "permanent"))
        return RecoveryPlan(
            error_id=str(data.get("error_id", error_id)),
            diagnosis=str(data.get("diagnosis", ""))[:500],
            category=category,
            action=str(data.get("action", "give_up")),
            args=data.get("args", {}) or {},
            risk_level=str(data.get("risk_level", "low")),
            requires_confirmation=bool(data.get("requires_confirmation", True)),
            retry_allowed=bool(data.get("retry_allowed", True)),
            reason=str(data.get("reason", ""))[:500],
            # The plan always came from this tier, whatever the payload says.
            tier=Tier.OPENCODE,
            source="opencode",
        )
    except Exception:
        return None
