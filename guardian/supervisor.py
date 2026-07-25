"""The guardian proper: the single gate between a plan and an actuator.

Contract:

* Input is an untrusted :class:`~common.models.Plan`.
* Output is always an :class:`~common.models.ApprovedPlan` - never an exception, never
  ``None``. If the plan is unusable the verdict is ``REJECTED`` and the returned steps are the
  baseline fallback. The simulation always has something safe to actuate.
* Every call is journalled as a :class:`~common.models.GuardianEvent`, including clean ones.
  "No violations" is a measurement, not silence.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from common.models import ApprovedPlan, BuildingState, GuardianEvent, Plan
from guardian.limits import DEFAULT_LIMITS, GuardianLimits


class Guardian:
    """Deterministic reviewer. Same inputs, same verdict, every time."""

    def __init__(self, limits: GuardianLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def review(self, plan: Plan, state: BuildingState) -> ApprovedPlan:
        """Clamp, rate-limit and validate ``plan``. Always returns an actuatable result.

        Order of checks matters and is part of the contract:
        schema/zone validity -> absolute bounds -> rate limits -> deadband -> comfort band.
        """
        raise NotImplementedError("guardian review not implemented yet (scaffold)")

    def journal(self, plan: Plan, approved: ApprovedPlan) -> GuardianEvent:
        """Build the journal row for a completed review."""
        raise NotImplementedError("guardian journalling not implemented yet (scaffold)")


__all__ = ["Guardian"]
