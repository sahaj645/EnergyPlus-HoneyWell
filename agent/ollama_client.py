"""Thin wrapper around a local Ollama server.

Design notes:

* ``ollama`` is imported lazily so that importing :mod:`agent` costs nothing and works on a
  machine with no model runtime (CI, the dashboard).
* Generation is **constrained** by ``Plan.model_json_schema()`` rather than by asking nicely
  for JSON in the prompt. Malformed plans should be impossible, not merely rare.
* Every call is bounded by a timeout. A planner that hangs must degrade to "no new plan",
  which the guardian already handles as "keep the previous approved plan".

Scaffold only: no logic yet.
"""

from __future__ import annotations

from common.models import Plan


class PlannerUnavailableError(RuntimeError):
    """Raised when Ollama is unreachable, times out, or returns nothing usable."""


class OllamaPlanner:
    """Turns a digest into a validated :class:`~common.models.Plan`."""

    def __init__(
        self,
        model: str = "qwen2.5:7b-instruct-q4_K_M",
        *,
        host: str = "http://localhost:11434",
        timeout_s: float = 30.0,
    ) -> None:
        self.model = model
        self.host = host
        self.timeout_s = timeout_s

    def health(self) -> bool:
        """Return True if the server is reachable and the model is pulled."""
        raise NotImplementedError("health check not implemented yet (scaffold)")

    def plan(self, digest: str, *, horizon_minutes: int = 60) -> Plan:
        """Generate one plan. Raises :class:`PlannerUnavailableError` on any failure.

        The caller never sees raw model output: the response is parsed and validated into a
        ``Plan`` here, or an exception is raised. It is still untrusted - the guardian rules
        on it next.
        """
        raise NotImplementedError("plan generation not implemented yet (scaffold)")


__all__ = ["OllamaPlanner", "PlannerUnavailableError"]
