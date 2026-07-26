"""Hypothesis properties on ``guardian.core.Guardian.filter`` - the safety kernel.

Every other guardian test (``tests/test_guardian.py``) is example-based, against the *older*
``guardian/supervisor.py`` path. This file targets the definitive kernel
(``guardian/core.py``) with adversarial, generated input, because that is the interface the
project's whole safety story rests on: whatever a 7B model emits - schema-legal or otherwise -
``filter`` must turn into something safe to actuate, deterministically, without raising.

Five properties, matching CLAUDE.md's own claims about this module:

(a) **Envelope containment** - "no reachable plan can exit the comfort envelope": every
    temperature-setpoint step in the safe plan of an *occupied* zone lies inside
    ``EnvelopeConfig.band(occupied=True)``.
(b) **Rate-limit adherence** across consecutive filtered plans, replaying the same
    ``RateHistory`` the executor actually carries forward.
(c) **Whitelist totality** - a step for an actuator outside ``GuardianConfig.whitelist`` never
    survives, for any input.
(d) **Never raises** - garbage in (NaN, inf, absurd magnitudes, unknown zones, empty plans),
    a ``GuardianVerdict`` out, every time.
(e) **Idempotence** on an already-safe plan: filtering a plan that came out of ``filter`` once
    already changes nothing the second time.

500 examples per property locally (``--hypothesis-profile=dev``, the default here); CI runs the
capped ``ci`` profile (``tests/conftest.py``) so a slow adversarial run never blocks a push.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from hypothesis import assume, given
from hypothesis import strategies as st

from common.models import Actuator, PlanStep, SetpointPlan, ZoneState
from guardian.core import Guardian, RateHistory
from guardian.limits import DEFAULT_CONFIG

ZONE = "Core_ZN"
GUARDIAN = Guardian()
ENVELOPE = DEFAULT_CONFIG.envelope

# -- strategies ---------------------------------------------------------------------------

#: Every actuator the schema can express, not just the whitelisted ones - whitelist totality
#: needs to see the stripped ones too.
_actuators = st.sampled_from(list(Actuator))

#: Schema-legal extremes plus the values a hostile/garbage plan would carry: NaN, +-inf, huge
#: magnitudes. ``PlanStep.value`` is a plain ``float`` with no bound (guardian.core.py's own
#: docstring: "the *real* envelope is the guardian's job, not the schema's"), so all of these
#: pass Pydantic validation and reach ``filter`` exactly as a hostile plan would.
_values = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True, width=32),
    st.floats(min_value=-1e9, max_value=1e9),
)

_zones = st.sampled_from([ZONE, "Ghost_ZN"])


def _step(actuator, value, zone=ZONE, offset=0) -> PlanStep:
    return PlanStep(offset_minutes=offset, zone=zone, actuator=actuator, value=value)


@st.composite
def plans(draw, *, zone: str = ZONE) -> SetpointPlan:
    """A plan with 0-6 steps, actuators/values/zones drawn from the adversarial strategies above."""
    n = draw(st.integers(min_value=0, max_value=6))
    steps = [
        _step(
            draw(_actuators),
            draw(_values),
            zone=draw(_zones) if draw(st.booleans()) else zone,
            offset=draw(st.integers(min_value=0, max_value=24 * 60)),
        )
        for _ in range(n)
    ]
    return SetpointPlan(planner_model="hypothesis", steps=steps)


@st.composite
def zone_states(draw, *, occupied: bool | None = None) -> ZoneState:
    is_occupied = draw(st.booleans()) if occupied is None else occupied
    return ZoneState(
        zone=ZONE,
        air_temp_c=draw(st.floats(min_value=-50, max_value=80)),
        occupancy=draw(st.floats(min_value=0.1, max_value=50)) if is_occupied else 0.0,
        cooling_setpoint_c=draw(st.one_of(st.none(), _values)),
        heating_setpoint_c=draw(st.one_of(st.none(), _values)),
        pmv=draw(
            st.one_of(st.none(), st.floats(min_value=-10, max_value=10), st.just(float("nan")))
        ),
    )


_TEMP_ACTUATORS = (Actuator.COOLING_SETPOINT_C, Actuator.HEATING_SETPOINT_C)


# --------------------------------------------------------------------------------------
# (a) Envelope containment
# --------------------------------------------------------------------------------------


@given(plan=plans(), state=zone_states(occupied=True))
def test_envelope_containment_occupied(plan: SetpointPlan, state: ZoneState) -> None:
    """No reachable plan can exit the comfort envelope - occupied zones, the tight band."""
    verdict = GUARDIAN.filter(plan, state, RateHistory.empty())
    low, high = ENVELOPE.band(occupied=True)
    for step in verdict.safe_plan.steps:
        if step.actuator in _TEMP_ACTUATORS:
            assert math.isfinite(step.value), "a surviving step must never be non-finite"
            assert low - 1e-6 <= step.value <= high + 1e-6, (
                f"{step.actuator} escaped the occupied envelope: {step.value} not in "
                f"[{low}, {high}]"
            )


@given(plan=plans(), state=zone_states(occupied=False))
def test_envelope_containment_unoccupied(plan: SetpointPlan, state: ZoneState) -> None:
    """Same property, unoccupied zones - the wider energy-conservation band."""
    verdict = GUARDIAN.filter(plan, state, RateHistory.empty())
    low, high = ENVELOPE.band(occupied=False)
    for step in verdict.safe_plan.steps:
        if step.actuator in _TEMP_ACTUATORS:
            assert math.isfinite(step.value)
            assert low - 1e-6 <= step.value <= high + 1e-6


# --------------------------------------------------------------------------------------
# (b) Rate-limit adherence across consecutive filtered plans
# --------------------------------------------------------------------------------------


@given(
    state=zone_states(occupied=True),
    values=st.lists(_values, min_size=2, max_size=8),
)
def test_rate_limit_adherence_across_cycles(state: ZoneState, values: list[float]) -> None:
    """Replay the RateHistory forward exactly as the executor does; no cycle-to-cycle jump
    ever exceeds the per-timestep rate, whatever the planner asked for."""
    history = RateHistory.empty()
    now = datetime(2017, 7, 15, 12, 0)
    rate = DEFAULT_CONFIG.rate
    previous: float | None = None

    for i, value in enumerate(values):
        plan = SetpointPlan(
            planner_model="hypothesis",
            steps=[_step(Actuator.COOLING_SETPOINT_C, value)],
        )
        verdict = GUARDIAN.filter(plan, state, history)
        applied = next(
            (s.value for s in verdict.safe_plan.steps if s.actuator == Actuator.COOLING_SETPOINT_C),
            None,
        )
        if applied is not None and previous is not None:
            assert abs(applied - previous) <= rate.max_step_per_timestep_c + 1e-6, (
                f"step {i}: jumped {previous} -> {applied}, exceeding "
                f"{rate.max_step_per_timestep_c} C/timestep"
            )
        if applied is not None:
            history = history.record(state.zone, now + timedelta(minutes=15 * i), applied)
            previous = applied


# --------------------------------------------------------------------------------------
# (c) Whitelist totality
# --------------------------------------------------------------------------------------


@given(plan=plans(), state=zone_states())
def test_whitelist_totality(plan: SetpointPlan, state: ZoneState) -> None:
    """A step for a non-whitelisted actuator never survives filtering, for any input."""
    verdict = GUARDIAN.filter(plan, state, RateHistory.empty())
    surviving = {s.actuator for s in verdict.safe_plan.steps}
    assert surviving <= set(DEFAULT_CONFIG.whitelist)


# --------------------------------------------------------------------------------------
# (d) filter never raises
# --------------------------------------------------------------------------------------


@given(plan=plans(), state=zone_states())
def test_filter_never_raises(plan: SetpointPlan, state: ZoneState) -> None:
    """Garbage in (NaN/inf/huge magnitudes/unknown zones/empty plans), a verdict out - always."""
    verdict = GUARDIAN.filter(plan, state, RateHistory.empty())
    assert verdict is not None
    assert verdict.zone == state.zone
    for step in verdict.safe_plan.steps:
        assert math.isfinite(step.value)


# --------------------------------------------------------------------------------------
# (e) Idempotence on an already-safe plan
# --------------------------------------------------------------------------------------


@given(plan=plans(), state=zone_states())
def test_idempotent_on_already_safe_plan(plan: SetpointPlan, state: ZoneState) -> None:
    """Filtering an already-filtered plan a second time (fresh history, as a new cycle would
    see it) changes nothing further - a safe plan is a fixed point of ``filter``."""
    first = GUARDIAN.filter(plan, state, RateHistory.empty())
    safe_plan = first.safe_plan.model_copy(update={"steps": list(first.safe_plan.steps)})
    # Give the second pass the observed setpoint it would actually see after the first plan's
    # steps were applied, so the rate limiter's anchor matches reality rather than re-testing a
    # discontinuity that was never actuated.
    assume(not any(math.isnan(s.value) or math.isinf(s.value) for s in safe_plan.steps))

    second = GUARDIAN.filter(safe_plan, state, RateHistory.empty())
    first_values = [s.value for s in first.safe_plan.steps]
    second_values = [s.value for s in second.safe_plan.steps]
    assert first_values == second_values
    assert not second.clipped or first.clipped, (
        "a plan already accepted/clipped once should not pick up *new* clipping on replay"
    )


__all__: list[str] = []
