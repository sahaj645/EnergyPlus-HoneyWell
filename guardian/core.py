"""The deterministic safety layer.

`Guardian.filter(plan, state, history)` is the single, pure entry point. Given a plan, one
zone's observed state, and that zone's rate history, it returns a :class:`GuardianVerdict`:
the safe plan for that zone, a status, and machine-usable reason strings. Same inputs, same
verdict, every time - no clock reads, no randomness, no I/O, and (rule for this package) no LLM
or network anywhere in `guardian/`.

Everything here is a pure function of its arguments precisely because property tests land on
this interface later. The one piece of state - how fast a setpoint has been moving - is carried
in an explicit :class:`RateHistory` passed in and returned anew, never hidden in the instance
or a module global.

Three protections, applied in a fixed order because each narrows what the next sees:

1. **Whitelist.** Actuators the guardian cannot drive are stripped and logged, never fatal.
2. **Comfort envelope**, selected by *observed* occupancy (never trusted from the plan):
   occupied -> a tight band around 23 C plus a PMV "do not make it worse" guard; unoccupied ->
   a wider energy-conservation band.
3. **Rate limit**, against the last applied value and the value ~an hour ago.

The verdict's ``safe_plan`` is a :class:`Plan`. Turning the surviving steps into the
:class:`ApprovedPlan` the actuator accepts is :meth:`Guardian.approve` - the guardian is the
only producer of ``ApprovedPlan`` (rule R2), so that construction lives here, not in the
executor.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.log import get_logger
from common.models import (
    Actuator,
    ApprovedPlan,
    GuardianDecision,
    GuardianStatus,
    GuardianVerdict,
    Plan,
    PlanStep,
    ZoneState,
)
from guardian.limits import DEFAULT_CONFIG, GuardianConfig

log = get_logger("guardian.core")

#: Actuators whose value is a zone-air-temperature setpoint, so the comfort envelope applies.
_TEMPERATURE_SETPOINTS = (Actuator.COOLING_SETPOINT_C, Actuator.HEATING_SETPOINT_C)

_HOUR = timedelta(hours=1)


def _fmt(value: float) -> str:
    return f"{value:.4g}"


# --------------------------------------------------------------------------------------
# Rate history — explicit, immutable, passed in (no hidden state)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RateSample:
    at: datetime
    value: float


@dataclass(frozen=True)
class RateHistory:
    """Per-zone trailing window of *applied* setpoint values, at most one hour long.

    Immutable: :meth:`record` returns a new instance with the sample added and anything older
    than an hour pruned. That purity is what lets property tests reason about the rate limiter
    without threading hidden state through the guardian.

    The window is pruned to an hour on record, so :meth:`oldest` is "the value roughly an hour
    ago" - which is exactly the reference the per-hour limit needs, and it means
    :meth:`Guardian.filter` never has to be told what time it is.
    """

    window: Mapping[str, tuple[RateSample, ...]] = field(default_factory=dict)
    hour: timedelta = _HOUR

    @classmethod
    def empty(cls, *, hour: timedelta = _HOUR) -> RateHistory:
        return cls(window={}, hour=hour)

    def last(self, zone: str) -> RateSample | None:
        samples = self.window.get(zone)
        return samples[-1] if samples else None

    def oldest(self, zone: str) -> RateSample | None:
        samples = self.window.get(zone)
        return samples[0] if samples else None

    def record(self, zone: str, at: datetime, value: float) -> RateHistory:
        """Return a new history with ``(at, value)`` appended for ``zone``, pruned to the hour."""
        existing = self.window.get(zone, ())
        cutoff = at - self.hour
        kept = tuple(s for s in existing if s.at > cutoff)
        updated = {**self.window, zone: (*kept, RateSample(at=at, value=value))}
        return RateHistory(window=updated, hour=self.hour)


# --------------------------------------------------------------------------------------
# The guardian
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _StepOutcome:
    """Internal: what happened to one step."""

    step: PlanStep | None  # None when stripped or rejected
    reasons: tuple[str, ...]
    rejected: bool = False


class Guardian:
    """Deterministic reviewer. Pure `filter`; explicit rate state; whitelist, envelope, rate."""

    def __init__(self, config: GuardianConfig = DEFAULT_CONFIG) -> None:
        self.config = config

    # -- the pure entry point ----------------------------------------------------------

    def filter(self, plan: Plan, state: ZoneState, history: RateHistory) -> GuardianVerdict:
        """Filter ``plan`` for the single zone described by ``state``.

        Pure: no mutation of ``history`` (the executor records the *applied* value afterwards),
        no I/O, no clock. Only steps whose ``zone`` matches ``state.zone`` are considered - a
        plan spanning several zones is filtered one zone per call, which is what keeps this a
        clean per-zone function the property tests can hammer.
        """
        occupied = self._is_occupied(state)
        reasons: list[str] = []
        safe_steps: list[PlanStep] = []
        any_stripped = False
        any_changed = False
        any_rejected = False

        for step in plan.steps:
            if step.zone != state.zone:
                continue

            outcome = self._filter_step(step, state, history, occupied=occupied)
            reasons.extend(outcome.reasons)

            if outcome.rejected:
                any_rejected = True
                continue
            if outcome.step is None:
                any_stripped = True
                continue
            if outcome.step.value != step.value:
                any_changed = True
            safe_steps.append(outcome.step)

        status = self._status(
            had_steps=any(s.zone == state.zone for s in plan.steps),
            safe_steps=safe_steps,
            changed=any_changed,
            stripped=any_stripped,
            rejected=any_rejected,
        )

        safe_plan = plan.model_copy(update={"steps": safe_steps})
        return GuardianVerdict(status=status, zone=state.zone, reasons=reasons, safe_plan=safe_plan)

    # -- per-step pipeline -------------------------------------------------------------

    def _filter_step(
        self, step: PlanStep, state: ZoneState, history: RateHistory, *, occupied: bool
    ) -> _StepOutcome:
        if not self.config.permits(step.actuator):
            return _StepOutcome(
                step=None,
                reasons=(f"strip: {state.zone} {step.actuator} whitelist",),
            )

        value = step.value
        if not math.isfinite(value):
            return _StepOutcome(
                step=None,
                reasons=(f"reject: {state.zone} {step.actuator} non_finite",),
                rejected=True,
            )

        reasons: list[str] = []

        if step.actuator in _TEMPERATURE_SETPOINTS:
            value = self._apply_envelope(value, state, reasons, occupied=occupied)
            value = self._apply_pmv(step.actuator, value, state, reasons, occupied=occupied)
            value = self._apply_rate(step.actuator, value, state, history, reasons)

        return _StepOutcome(step=step.model_copy(update={"value": value}), reasons=tuple(reasons))

    def _apply_envelope(
        self, value: float, state: ZoneState, reasons: list[str], *, occupied: bool
    ) -> float:
        low, high = self.config.envelope.band(occupied=occupied)
        if value < low:
            reasons.append(f"clip: {state.zone} {_fmt(value)}->{_fmt(low)} envelope_min")
            return low
        if value > high:
            reasons.append(f"clip: {state.zone} {_fmt(value)}->{_fmt(high)} envelope_max")
            return high
        return value

    def _apply_pmv(
        self,
        actuator: Actuator,
        value: float,
        state: ZoneState,
        reasons: list[str],
        *,
        occupied: bool,
    ) -> float:
        """Refuse to make observed thermal discomfort worse. Occupied hours only.

        The guardian does not compute the setpoint that yields a target PMV - that needs a full
        comfort model and is the planner's job. It only clamps *directional* moves: when the
        room already reads too warm, do not let a setpoint rise (which would add heat or cut
        cooling); when it reads too cold, do not let one fall.
        """
        if not occupied or state.pmv is None:
            return value
        current = self._observed_setpoint(actuator, state)
        if current is None:
            return value

        env = self.config.envelope
        too_warm = state.pmv > env.pmv_target + env.pmv_tolerance
        too_cold = state.pmv < env.pmv_target - env.pmv_tolerance

        if too_warm and value > current:
            reasons.append(f"clip: {state.zone} {_fmt(value)}->{_fmt(current)} pmv_hot")
            return current
        if too_cold and value < current:
            reasons.append(f"clip: {state.zone} {_fmt(value)}->{_fmt(current)} pmv_cold")
            return current
        return value

    def _apply_rate(
        self,
        actuator: Actuator,
        value: float,
        state: ZoneState,
        history: RateHistory,
        reasons: list[str],
    ) -> float:
        """Clamp against the last applied value (per-timestep) and the value ~1 h ago (per-hour).

        With no history yet, the *observed* setpoint is the anchor - so the very first
        application cannot leap either, which is what catches an instant jump on step one.
        """
        rate = self.config.rate

        last = history.last(state.zone)
        anchor = last.value if last is not None else self._observed_setpoint(actuator, state)
        if anchor is not None:
            limited = _clamp(value, anchor - rate.max_step_per_timestep_c,
                             anchor + rate.max_step_per_timestep_c)
            if limited != value:
                reasons.append(f"rate: {state.zone} {_fmt(value)}->{_fmt(limited)} rate_step")
                value = limited

        oldest = history.oldest(state.zone)
        if oldest is not None:
            limited = _clamp(value, oldest.value - rate.max_step_per_hour_c,
                             oldest.value + rate.max_step_per_hour_c)
            if limited != value:
                reasons.append(f"rate: {state.zone} {_fmt(value)}->{_fmt(limited)} rate_hour")
                value = limited

        return value

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _is_occupied(state: ZoneState) -> bool:
        """Occupancy from the observation, never from the plan."""
        return (state.occupancy or 0.0) > 0.0

    @staticmethod
    def _observed_setpoint(actuator: Actuator, state: ZoneState) -> float | None:
        if actuator is Actuator.COOLING_SETPOINT_C:
            return state.cooling_setpoint_c
        if actuator is Actuator.HEATING_SETPOINT_C:
            return state.heating_setpoint_c
        return None

    @staticmethod
    def _status(
        *,
        had_steps: bool,
        safe_steps: list[PlanStep],
        changed: bool,
        stripped: bool,
        rejected: bool,
    ) -> GuardianStatus:
        if had_steps and not safe_steps:
            # Every step for this zone was stripped or rejected - nothing safe to actuate.
            return GuardianStatus.REJECTED
        if changed or stripped or rejected:
            return GuardianStatus.CLIPPED
        return GuardianStatus.ACCEPTED

    # -- ApprovedPlan construction (rule R2 lives here) --------------------------------

    def approve(
        self,
        verdicts: Sequence[GuardianVerdict],
        *,
        plan_id: str,
        now: datetime,
        fallback: bool = False,
    ) -> ApprovedPlan:
        """Assemble per-zone verdicts into the one object the actuator accepts.

        The only producer of :class:`ApprovedPlan` in the codebase (rule R2). Steps are emitted
        at ``offset_minutes=0`` - the executor has already resolved "what value applies now",
        so the actuator writes them immediately.
        """
        steps: list[PlanStep] = []
        for verdict in verdicts:
            for step in verdict.safe_plan.steps:
                steps.append(step.model_copy(update={"offset_minutes": 0}))

        if any(v.rejected for v in verdicts):
            decision = GuardianDecision.REJECTED
        elif any(v.clipped for v in verdicts):
            decision = GuardianDecision.CLAMPED
        else:
            decision = GuardianDecision.ACCEPTED

        return ApprovedPlan(
            plan_id=plan_id,
            approved_at=now,
            decision=decision,
            steps=steps,
            fallback=fallback,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


__all__ = ["Guardian", "RateHistory", "RateSample"]
