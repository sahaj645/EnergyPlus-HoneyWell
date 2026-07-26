"""The guardian proper: the single gate between a plan and an actuator.

Contract:

* Input is an untrusted :class:`~common.models.Plan`.
* Output is always an :class:`~common.models.ApprovedPlan` - never an exception, never
  ``None``. If the plan is unusable the verdict is ``REJECTED`` and the caller falls back.
  The simulation always has something safe to actuate.
* Every call is journallable as a :class:`~common.models.GuardianEvent`, including clean ones.
  "No violations" is a measurement, not silence.
* Deterministic: same plan, same state, same verdict. No clock reads, no randomness, no I/O.

**Scope of this first cut.** It enforces the checks that can be decided from a plan plus the
current :class:`~common.models.BuildingState`: unknown zones, absolute bounds, per-cycle rate
limits (measured against the *observed* setpoint, which is the only honest baseline), the
heating/cooling deadband, and the occupied comfort band. Plan staleness and planner liveness
belong to :mod:`guardian.watchdog` and are still stubs - the caller is responsible for not
handing a stale plan to ``review`` until that lands.
"""

from __future__ import annotations

from common.models import (
    Actuator,
    ApprovedPlan,
    BuildingState,
    GuardianDecision,
    GuardianEvent,
    PlanStep,
    SetpointPlan,
    Violation,
    ViolationCode,
)
from guardian.limits import DEFAULT_LIMITS, GuardianLimits

#: Actuators whose value is a zone air temperature setpoint, so the comfort band applies.
_TEMPERATURE_SETPOINTS = (Actuator.COOLING_SETPOINT_C, Actuator.HEATING_SETPOINT_C)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class Guardian:
    """Deterministic reviewer. Same inputs, same verdict, every time."""

    def __init__(self, limits: GuardianLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def review(self, plan: SetpointPlan, state: BuildingState) -> ApprovedPlan:
        """Clamp, rate-limit and validate ``plan``. Always returns an actuatable result.

        Order of checks is part of the contract, because each stage's output feeds the next:
        zone validity -> absolute bounds -> rate limit -> comfort band -> deadband.
        """
        known_zones = {zone.zone for zone in state.zones}
        observed = {zone.zone: zone for zone in state.zones}

        violations: list[Violation] = []
        approved: list[PlanStep] = []

        for index, step in enumerate(plan.steps):
            if step.zone not in known_zones:
                violations.append(
                    Violation(
                        code=ViolationCode.UNKNOWN_ZONE,
                        message=f"zone {step.zone!r} is not in the model",
                        step_index=index,
                        original_value=step.value,
                    )
                )
                continue

            value = step.value

            value = self._apply_bounds(step, index, value, violations)
            value = self._apply_rate_limit(step, index, value, observed, violations)
            value = self._apply_comfort_band(step, index, value, violations)

            approved.append(step.model_copy(update={"value": value}))

        approved, deadband_violations = self._enforce_deadband(approved, observed)
        violations.extend(deadband_violations)

        decision = self._decide(plan, approved, violations)
        return ApprovedPlan(
            plan_id=plan.plan_id,
            decision=decision,
            steps=approved if decision is not GuardianDecision.REJECTED else [],
            violations=violations,
            fallback=decision is GuardianDecision.REJECTED,
        )

    # -- individual checks -------------------------------------------------------------

    def _apply_bounds(
        self, step: PlanStep, index: int, value: float, violations: list[Violation]
    ) -> float:
        bound = self.limits.bounds.get(step.actuator)
        if bound is None:
            return value
        clamped = _clamp(value, bound.minimum, bound.maximum)
        if clamped != value:
            violations.append(
                Violation(
                    code=ViolationCode.OUT_OF_RANGE,
                    message=(
                        f"{step.actuator} {value} outside "
                        f"[{bound.minimum}, {bound.maximum}] for {step.zone}"
                    ),
                    step_index=index,
                    original_value=value,
                    clamped_value=clamped,
                )
            )
        return clamped

    def _apply_rate_limit(
        self,
        step: PlanStep,
        index: int,
        value: float,
        observed: dict[str, object],
        violations: list[Violation],
    ) -> float:
        bound = self.limits.bounds.get(step.actuator)
        current = self._observed_value(step, observed)
        if bound is None or current is None:
            return value

        delta = value - current
        if abs(delta) <= bound.max_step:
            return value

        limited = current + (bound.max_step if delta > 0 else -bound.max_step)
        violations.append(
            Violation(
                code=ViolationCode.RATE_LIMIT,
                message=(
                    f"{step.actuator} moves {delta:+.2f} in one cycle for {step.zone}; "
                    f"limit is {bound.max_step}"
                ),
                step_index=index,
                original_value=value,
                clamped_value=limited,
            )
        )
        return limited

    def _apply_comfort_band(
        self, step: PlanStep, index: int, value: float, violations: list[Violation]
    ) -> float:
        if step.actuator not in _TEMPERATURE_SETPOINTS:
            return value
        clamped = _clamp(value, self.limits.comfort_min_c, self.limits.comfort_max_c)
        if clamped != value:
            violations.append(
                Violation(
                    code=ViolationCode.COMFORT_BAND,
                    message=(
                        f"{step.actuator} {value} outside occupied comfort band "
                        f"[{self.limits.comfort_min_c}, {self.limits.comfort_max_c}] "
                        f"for {step.zone}"
                    ),
                    step_index=index,
                    original_value=value,
                    clamped_value=clamped,
                )
            )
        return clamped

    def _enforce_deadband(
        self, steps: list[PlanStep], observed: dict[str, object]
    ) -> tuple[list[PlanStep], list[Violation]]:
        """Keep cooling at least ``min_deadband_c`` above heating, per zone and offset.

        Heating yields, not cooling: pushing the cooling setpoint down to make room would
        increase energy use, which is the opposite of what any plan here is trying to do.
        """
        violations: list[Violation] = []
        by_slot: dict[tuple[str, int], dict[Actuator, int]] = {}
        for index, step in enumerate(steps):
            by_slot.setdefault((step.zone, step.offset_minutes), {})[step.actuator] = index

        updated = list(steps)
        for (zone, _offset), slot in by_slot.items():
            cool_index = slot.get(Actuator.COOLING_SETPOINT_C)
            heat_index = slot.get(Actuator.HEATING_SETPOINT_C)

            cooling = (
                updated[cool_index].value
                if cool_index is not None
                else self._observed_setpoint(zone, observed, "cooling_setpoint_c")
            )
            heating = (
                updated[heat_index].value
                if heat_index is not None
                else self._observed_setpoint(zone, observed, "heating_setpoint_c")
            )
            if cooling is None or heating is None:
                continue

            gap = cooling - heating
            if gap >= self.limits.min_deadband_c:
                continue

            corrected = cooling - self.limits.min_deadband_c
            if heat_index is None:
                # Only cooling was planned and it crowds the existing heating setpoint; we
                # cannot fix that by moving a step that does not exist, so flag it.
                violations.append(
                    Violation(
                        code=ViolationCode.DEADBAND,
                        message=(
                            f"cooling {cooling} leaves {gap:.2f} C above heating {heating} "
                            f"in {zone}; minimum is {self.limits.min_deadband_c}"
                        ),
                        original_value=cooling,
                    )
                )
                continue

            violations.append(
                Violation(
                    code=ViolationCode.DEADBAND,
                    message=(
                        f"heating lowered to preserve a {self.limits.min_deadband_c} C "
                        f"deadband below cooling {cooling} in {zone}"
                    ),
                    step_index=heat_index,
                    original_value=heating,
                    clamped_value=corrected,
                )
            )
            updated[heat_index] = updated[heat_index].model_copy(
                update={"value": corrected}
            )

        return updated, violations

    @staticmethod
    def _observed_setpoint(zone: str, observed: dict[str, object], attribute: str) -> float | None:
        record = observed.get(zone)
        return getattr(record, attribute, None) if record is not None else None

    @classmethod
    def _observed_value(cls, step: PlanStep, observed: dict[str, object]) -> float | None:
        attribute = {
            Actuator.COOLING_SETPOINT_C: "cooling_setpoint_c",
            Actuator.HEATING_SETPOINT_C: "heating_setpoint_c",
        }.get(step.actuator)
        if attribute is None:
            return None
        return cls._observed_setpoint(step.zone, observed, attribute)

    def _decide(
        self, plan: SetpointPlan, approved: list[PlanStep], violations: list[Violation]
    ) -> GuardianDecision:
        if plan.steps and not approved:
            return GuardianDecision.REJECTED
        if violations:
            return GuardianDecision.CLAMPED
        return GuardianDecision.ACCEPTED

    # -- journalling -------------------------------------------------------------------

    def journal(self, plan: SetpointPlan, approved: ApprovedPlan) -> GuardianEvent:
        """Build the journal row for a completed review."""
        return GuardianEvent(
            plan_id=plan.plan_id,
            decision=approved.decision,
            violations=approved.violations,
            note=(
                f"{len(approved.steps)}/{len(plan.steps)} steps approved, "
                f"{len(approved.violations)} violation(s)"
            ),
        )


__all__ = ["Guardian"]
