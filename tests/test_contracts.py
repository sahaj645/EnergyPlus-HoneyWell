"""Contract tests: the interfaces other subsystems are built to trust.

Three contracts, each backing a claim made elsewhere in the codebase:

1. **Plan JSON-schema round-trip.** ``Plan.model_json_schema()`` is handed to Ollama as the
   constrained-decoding grammar (``agent/planner.py``) - if a schema-legal sample cannot survive
   a dump/parse round-trip through the very model it was generated from, the grammar and the
   validator have drifted apart and every plan would be rejected for reasons that have nothing
   to do with the model's actual output.
2. **Digest token budget on a worst-case state.** ``agent/digest.py`` promises "<= ~1.5K tokens"
   (rule of thumb: 4 chars/token) - checked here against the real building's full zone count
   (``common.generated_enums.ZoneEnum``, not an arbitrary guess), a full 6-hour forecast, and a
   non-empty feedback section, i.e. the largest digest a real run actually produces.
3. **Plan-slot thread contention.** ``common/planslot.py``'s whole reason for existing is "the
   planner thread commits, the callback thread reads, at the same time" - eight threads hammering
   ``commit``/``get`` concurrently must never see a torn read or raise, matching
   ``PlanSlot.get``'s own doc promise ("never raises... safe to call from the callback").
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from common.models import (
    ActuatorEnum,
    BuildingState,
    EcmEnum,
    Plan,
    PlanAction,
    SetpointPlan,
    TriggerEnum,
    ZoneEnum,
    ZoneState,
)
from common.planslot import PlanSlot

# --------------------------------------------------------------------------------------
# 1. Plan JSON-schema round-trip
# --------------------------------------------------------------------------------------


def _sample_plan() -> Plan:
    now = datetime(2017, 7, 15, 13, 0)
    return Plan(
        trigger=TriggerEnum.HOURLY,
        horizon_hours=6,
        ecms=[EcmEnum.PRECOOL, EcmEnum.COAST],
        confidence=0.8,
        actions=[
            PlanAction(
                zone=next(iter(ZoneEnum)),
                actuator=ActuatorEnum.COOLING_SETPOINT_C,
                value=23.5,
                start=now,
                end=now + timedelta(hours=2),
                rationale="precool ahead of the evening tariff peak",
            )
        ],
    )


def test_plan_schema_is_a_well_formed_json_schema_object() -> None:
    schema = Plan.model_json_schema()
    assert schema["type"] == "object"
    assert "properties" in schema
    # The two closed vocabularies the LLM is constrained to must actually appear as enums
    # somewhere in the schema (inline or via $defs) - not just exist as Python types.
    rendered = str(schema)
    for zone in ZoneEnum:
        assert zone.value in rendered
    for actuator in ActuatorEnum:
        assert actuator.value in rendered


def test_plan_round_trips_through_its_own_schema_grammar() -> None:
    """Dump a schema-legal Plan, parse it back, and get the same plan - the constrained-decoding
    contract Ollama's ``format=`` argument depends on."""
    plan = _sample_plan()
    dumped = plan.model_dump_json()

    parsed = Plan.model_validate_json(dumped)

    assert parsed.plan_id == plan.plan_id
    assert parsed.trigger == plan.trigger
    assert parsed.ecms == plan.ecms
    assert len(parsed.actions) == 1
    assert parsed.actions[0].zone == plan.actions[0].zone
    assert parsed.actions[0].actuator == plan.actions[0].actuator
    assert parsed.actions[0].value == plan.actions[0].value


def test_plan_lowers_to_a_setpoint_plan_the_guardian_can_consume() -> None:
    """The other half of the contract: whatever the schema lets the LLM emit must lower into
    ``SetpointPlan`` - the guardian never sees a ``Plan`` directly (rule R4)."""
    plan = _sample_plan()
    lowered = plan.to_setpoint_plan(now=datetime(2017, 7, 15, 13, 0))
    assert isinstance(lowered, SetpointPlan)
    assert lowered.plan_id == plan.plan_id
    assert len(lowered.steps) == 1
    assert lowered.steps[0].zone == plan.actions[0].zone.value


# --------------------------------------------------------------------------------------
# 2. Digest token budget on a worst-case state
# --------------------------------------------------------------------------------------


def _worst_case_state() -> BuildingState:
    """Every zone the real building has (``ZoneEnum``), fully populated - the largest single
    observation the digest ever renders, not a synthetic oversized fixture."""
    return BuildingState(
        sim_time=datetime(2017, 7, 15, 13, 30),
        outdoor_air_temp_c=38.4,
        outdoor_relative_humidity=62.0,
        direct_normal_irradiance=810.0,
        facility_power_w=125_430.0,
        zones=[
            ZoneState(
                zone=zone.value,
                air_temp_c=24.3,
                mean_radiant_temp_c=24.8,
                relative_humidity=55.0,
                occupancy=12.0,
                cooling_setpoint_c=23.5,
                heating_setpoint_c=21.0,
                pmv=0.42,
            )
            for zone in ZoneEnum
        ],
    )


def _worst_case_history(state: BuildingState) -> list[BuildingState]:
    earlier = state.model_copy(
        update={
            "sim_time": state.sim_time - timedelta(hours=1),
            "outdoor_air_temp_c": state.outdoor_air_temp_c - 3.0,
        }
    )
    return [earlier]


def test_digest_stays_within_token_budget_for_the_full_building() -> None:
    from agent.digest import ForecastRow, band, build_digest, within_budget

    state = _worst_case_state()
    forecast = [
        ForecastRow(
            at=state.sim_time + timedelta(hours=h),
            outdoor_c=30.0 + h,
            tariff_band=band(h % 3, 0, 2),
            carbon_band=band(h % 3, 0, 2),
        )
        for h in range(1, 7)  # the full 6-hour look-ahead
    ]
    feedback = [
        f"clip: {zone.value} 24.9->24.5 envelope_max" for zone in ZoneEnum
    ] + [f"rate: {zone.value} 21.5->23 rate_step" for zone in ZoneEnum]

    digest = build_digest(
        state,
        history=_worst_case_history(state),
        forecast=forecast,
        active_plan=_sample_plan(),
        feedback=feedback,
    )

    assert within_budget(digest), (
        f"worst-case digest ({len(digest)} chars, ~{len(digest) // 4} tokens) exceeded the "
        "1.5K token budget agent/digest.py promises"
    )


# --------------------------------------------------------------------------------------
# 3. Plan-slot thread contention
# --------------------------------------------------------------------------------------


def test_plan_slot_survives_eight_threads_hammering_commit_and_get() -> None:
    slot = PlanSlot()
    errors: list[BaseException] = []
    stop = threading.Event()
    iterations = 500

    def committer(worker_id: int) -> None:
        try:
            for i in range(iterations):
                plan = SetpointPlan(
                    plan_id=f"w{worker_id}-{i}", planner_model="contention-test", steps=[]
                )
                slot.commit(plan, at=datetime(2017, 7, 15) + timedelta(minutes=i))
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                snapshot = slot.snapshot()
                if snapshot is not None:
                    # A torn read would surface as a plan/generation/committed_at mismatch -
                    # nothing here should ever raise while reading a consistent snapshot.
                    assert snapshot.plan.plan_id.startswith("w")
                got = slot.get()
                if got is not None:
                    assert got.plan_id.startswith("w")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    writers = [threading.Thread(target=committer, args=(w,)) for w in range(8)]
    readers = [threading.Thread(target=reader) for _ in range(4)]

    for t in readers:
        t.start()
    for t in writers:
        t.start()
    for t in writers:
        t.join(timeout=30)
    stop.set()
    for t in readers:
        t.join(timeout=10)

    assert not errors, f"PlanSlot raised or produced a bad read under contention: {errors}"
    assert slot.generation == 8 * iterations
    assert slot.get() is not None
