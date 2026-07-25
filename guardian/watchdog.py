"""Watchdog: liveness checks that run outside the callback.

Three things it watches:

1. **Plan freshness** - is the cached plan older than the limit?
2. **Planner health** - is Ollama answering at all?
3. **Actuation sanity** - are commanded values actually showing up in telemetry?

Tripping the watchdog does not stop the simulation. It forces the fallback and records why,
because a run that finishes on baseline is far more useful than a run that died.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from datetime import datetime

from common.models import ViolationCode


class Watchdog:
    """Tracks planner liveness and plan freshness."""

    def __init__(
        self,
        *,
        max_plan_age_minutes: int = 30,
        max_consecutive_failures: int = 3,
    ) -> None:
        self.max_plan_age_minutes = max_plan_age_minutes
        self.max_consecutive_failures = max_consecutive_failures

    def record_success(self, at: datetime) -> None:
        """Note a successful planning cycle."""
        raise NotImplementedError("watchdog success path not implemented yet (scaffold)")

    def record_failure(self, at: datetime, reason: str) -> None:
        """Note a failed planning cycle."""
        raise NotImplementedError("watchdog failure path not implemented yet (scaffold)")

    def tripped(self, now: datetime) -> ViolationCode | None:
        """Return the reason the fallback should take over, or ``None`` if healthy."""
        raise NotImplementedError("watchdog evaluation not implemented yet (scaffold)")


__all__ = ["Watchdog"]
