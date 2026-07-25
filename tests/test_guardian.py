"""Guardian review: hostile plans in, safe plans out.

Rule R2 says to test the guardian by feeding it hostile plans rather than by going around it,
so that is what these do. Every fixture is built from :mod:`common.models` types (rule R4).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from common.models import (
    Actuator,
    BuildingState,
    GuardianDecision,
    Plan,
    PlanStep,
    ViolationCode,
    ZoneState,
)
from guardian.limits import DEFAULT_LIMITS
from guardian.supervisor import Guardian

ZONE = "Core_ZN"


def state(cooling: float = 24.0, heating: float = 21.0) -> BuildingState:
    return BuildingState(
        sim_time=datetime(2017, 7, 15, 12, 0),
        outdoor_air_temp_c=33.0,
        facility_power_w=5000.0,
        zones=[
            ZoneState(
                zone=ZONE,
                air_temp_c=24.5,
                cooling_setpoint_c=cooling,
                heating_setpoint_c=heating,
            )
        ],
    )


def plan_with(*steps: PlanStep) -> Plan:
    return Plan(planner_model="test", steps=list(steps))


def step(value: float, *, actuator: Actuator = Actuator.COOLING_SETPOINT_C, zone: str = ZONE,
         offset: int = 0) -> PlanStep:
    return PlanStep(offset_minutes=offset, zone=zone, actuator=actuator, value=value)


def codes(approved) -> set[ViolationCode]:
    return {v.code for v in approved.violations}


# --------------------------------------------------------------------------------------


def test_clean_plan_is_accepted_unchanged() -> None:
    approved = Guardian().review(plan_with(step(25.0)), state())
    assert approved.decision is GuardianDecision.ACCEPTED
    assert approved.violations == []
    assert approved.steps[0].value == pytest.approx(25.0)
    assert approved.fallback is False


def test_unknown_zone_is_dropped() -> None:
    approved = Guardian().review(plan_with(step(25.0, zone="Nonexistent_ZN")), state())
    assert ViolationCode.UNKNOWN_ZONE in codes(approved)
    assert approved.steps == []
    assert approved.decision is GuardianDecision.REJECTED
    assert approved.fallback is True


def test_absurd_setpoint_is_clamped_to_the_envelope() -> None:
    # 200 C would cook the occupants; the bound is 28.
    approved = Guardian().review(plan_with(step(200.0)), state())
    assert ViolationCode.OUT_OF_RANGE in codes(approved)
    bound = DEFAULT_LIMITS.bounds[Actuator.COOLING_SETPOINT_C]
    assert approved.steps[0].value <= bound.maximum
    assert approved.decision is GuardianDecision.CLAMPED


def test_negative_setpoint_is_clamped_up() -> None:
    approved = Guardian().review(plan_with(step(-40.0)), state())
    bound = DEFAULT_LIMITS.bounds[Actuator.COOLING_SETPOINT_C]
    assert approved.steps[0].value >= bound.minimum


def test_rate_limit_measured_against_the_observed_setpoint() -> None:
    # Observed cooling is 24.0; max_step is 1.5, so a jump to 28 becomes 25.5.
    approved = Guardian().review(plan_with(step(28.0)), state(cooling=24.0))
    assert ViolationCode.RATE_LIMIT in codes(approved)
    assert approved.steps[0].value == pytest.approx(25.5)


def test_small_move_is_not_rate_limited() -> None:
    approved = Guardian().review(plan_with(step(25.0)), state(cooling=24.0))
    assert ViolationCode.RATE_LIMIT not in codes(approved)


def test_comfort_band_caps_the_setpoint() -> None:
    # Bound allows 28, but the occupied comfort band caps at 27.
    approved = Guardian().review(plan_with(step(28.0)), state(cooling=27.0))
    assert approved.steps[0].value <= DEFAULT_LIMITS.comfort_max_c


def test_deadband_lowers_heating_not_cooling() -> None:
    """Heating yields: dropping cooling instead would raise energy use."""
    guardian = Guardian()
    plan = plan_with(
        step(23.0, actuator=Actuator.COOLING_SETPOINT_C),
        step(22.5, actuator=Actuator.HEATING_SETPOINT_C),
    )
    approved = guardian.review(plan, state(cooling=23.0, heating=22.0))

    by_actuator = {s.actuator: s.value for s in approved.steps}
    cooling = by_actuator[Actuator.COOLING_SETPOINT_C]
    heating = by_actuator[Actuator.HEATING_SETPOINT_C]

    assert cooling - heating >= DEFAULT_LIMITS.min_deadband_c
    assert cooling == pytest.approx(23.0), "cooling must not be sacrificed to the deadband"
    assert ViolationCode.DEADBAND in codes(approved)


def test_empty_plan_is_accepted_and_actuates_nothing() -> None:
    approved = Guardian().review(plan_with(), state())
    assert approved.decision is GuardianDecision.ACCEPTED
    assert approved.steps == []


def test_review_is_deterministic() -> None:
    guardian = Guardian()
    plan = plan_with(step(31.0), step(19.0, actuator=Actuator.HEATING_SETPOINT_C))
    first = guardian.review(plan, state())
    second = guardian.review(plan, state())
    assert [s.value for s in first.steps] == [s.value for s in second.steps]
    assert codes(first) == codes(second)


def test_review_never_raises_on_hostile_input() -> None:
    """Whatever the planner emits, the guardian must return something actuatable."""
    guardian = Guardian()
    hostile = plan_with(
        step(1e6),
        step(-1e6, actuator=Actuator.HEATING_SETPOINT_C),
        step(0.5, actuator=Actuator.FAN_FLOW_FRACTION),
        step(42.0, zone="Ghost_ZN"),
    )
    approved = guardian.review(hostile, state())
    assert approved is not None
    for approved_step in approved.steps:
        bound = DEFAULT_LIMITS.bounds.get(approved_step.actuator)
        if bound:
            assert bound.minimum <= approved_step.value <= bound.maximum


def test_journal_summarises_the_review() -> None:
    guardian = Guardian()
    plan = plan_with(step(200.0))
    approved = guardian.review(plan, state())
    event = guardian.journal(plan, approved)
    assert event.plan_id == plan.plan_id
    assert event.decision is approved.decision
    assert event.violations == approved.violations
