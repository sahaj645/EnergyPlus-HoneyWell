"""Baseline fallback: what we actuate when the agent cannot be trusted.

Reached whenever the plan is absent, rejected, or stale, the watchdog trips, or the callback
catches an exception (CLAUDE.md, rule R1). It is what lets the guardian **run the building
indefinitely with no planner at all** - the baseline schedule is a complete, safe control
policy on its own.

The baseline is the set of setpoints ``prepare_idf`` initialised each ``Schedule:Constant`` to,
carried in :attr:`PreparedModel.constant_schedules`. Emitting them holds the building at its
designed comfort condition. This is also the A/B control arm, so "agent disabled" and "agent
failed" produce identical behaviour.

It must be the dullest code in the repo: no I/O, no allocation surprises, and - because callers
are often inside an exception handler - **no chance of raising**.
"""

from __future__ import annotations

from datetime import datetime

from common.log import get_logger
from common.models import (
    Actuator,
    ApprovedPlan,
    GuardianDecision,
    PlanStep,
    PreparedModel,
)

log = get_logger("guardian.fallback")

#: ZoneBinding attribute -> the actuator its schedule drives.
_SCHEDULE_ACTUATORS: tuple[tuple[str, Actuator], ...] = (
    ("cooling_schedule", Actuator.COOLING_SETPOINT_C),
    ("heating_schedule", Actuator.HEATING_SETPOINT_C),
)


def baseline_steps(model: PreparedModel) -> list[PlanStep]:
    """One step per (zone, wired actuator), set to that schedule's baseline value.

    Skips any binding whose schedule is missing from ``constant_schedules`` rather than
    guessing - a zone with no known baseline simply contributes no step.
    """
    steps: list[PlanStep] = []
    for binding in model.zones:
        for attribute, actuator in _SCHEDULE_ACTUATORS:
            schedule = getattr(binding, attribute, None)
            if not schedule:
                continue
            value = model.constant_schedules.get(schedule)
            if value is None:
                continue
            steps.append(
                PlanStep(offset_minutes=0, zone=binding.zone, actuator=actuator, value=float(value))
            )
    return steps


def baseline_plan(
    model: PreparedModel,
    now: datetime,
    *,
    plan_id: str = "baseline-fallback",
    reason: str = "fallback",
) -> ApprovedPlan:
    """Return the baseline schedule as an already-approved, actuatable plan.

    ``fallback=True`` marks these as the baseline, not the planner's, so the dashboard and the
    journal can tell "the agent chose this" from "the agent was not driving". Never raises.
    """
    try:
        steps = baseline_steps(model)
    except Exception:
        # Truly last-resort: an empty plan holds whatever the schedules currently are, which is
        # still the baseline. Better a no-op than an exception crossing the C boundary.
        log.exception("baseline_steps failed; emitting an empty fallback plan (reason=%s)", reason)
        steps = []

    return ApprovedPlan(
        plan_id=plan_id,
        approved_at=now,
        decision=GuardianDecision.ACCEPTED,
        steps=steps,
        fallback=True,
    )


__all__ = ["baseline_plan", "baseline_steps"]
