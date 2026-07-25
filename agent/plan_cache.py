"""Plan cache: the hand-off between the planner thread and the EnergyPlus callback.

This is the only shared mutable state between the two, and it exists precisely so the
callback never has to wait for the model (CLAUDE.md, rule R1). The planner *deposits*; the
callback *reads the latest, without blocking*. A read must never acquire a lock the planner
could be holding across an inference call.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from common.models import ApprovedPlan


class PlanCache:
    """Latest-wins slot holding the most recent guardian-approved plan."""

    def __init__(self, *, max_age: timedelta = timedelta(minutes=30)) -> None:
        self.max_age = max_age

    def put(self, approved: ApprovedPlan) -> None:
        """Store a newly approved plan, replacing whatever was there."""
        raise NotImplementedError("plan cache write not implemented yet (scaffold)")

    def get(self, now: datetime) -> ApprovedPlan | None:
        """Return the current plan, or ``None`` if empty or older than ``max_age``.

        Must be non-blocking and must never raise: the callback calls this.
        """
        raise NotImplementedError("plan cache read not implemented yet (scaffold)")

    def is_stale(self, now: datetime) -> bool:
        """True when the held plan has aged out and the fallback should take over."""
        raise NotImplementedError("staleness check not implemented yet (scaffold)")


__all__ = ["PlanCache"]
