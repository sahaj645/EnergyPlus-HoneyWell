"""Baseline fallback: what we actuate when the agent cannot be trusted.

Reached whenever the plan is rejected, the plan is stale, the planner is unreachable, the
watchdog trips, or the callback catches an exception (CLAUDE.md, rule R1). It must be the
dullest code in the repo: no allocation surprises, no I/O, no chance of raising.

The fallback is the unmodified baseline schedule from the IDF - which is also the A/B control
arm, so "agent disabled" and "agent failed" produce the same building behaviour.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from datetime import datetime

from common.models import ApprovedPlan


def baseline_plan(now: datetime, reason: str = "fallback") -> ApprovedPlan:
    """Return the baseline schedule as an already-approved plan.

    Must never raise. Callers are in exception handlers.
    """
    raise NotImplementedError("baseline fallback not implemented yet (scaffold)")


__all__ = ["baseline_plan"]
