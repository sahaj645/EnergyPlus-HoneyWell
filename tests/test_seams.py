"""Unit tests for seams: places two subsystems meet and a boundary-off-by-one hides easily.

* **Cache key discretization boundaries** (``agent/cache.py``) - the exact hour/count/temperature
  values where ``hour_band``/``occupancy_bucket``/``outdoor_bin`` flip from one bucket to the
  next. A coarse cache key is only useful if the boundaries are where the docstring says they
  are.
* **Guardian rate-history across restarts** (``guardian/core.py``) - a process restart loses the
  in-memory ``RateHistory``; the rate limiter must fall back to the *observed* setpoint as its
  anchor rather than silently allowing an unlimited first jump.
* **L2 feedback-injection** (``agent/feedback.py`` + ``agent/scheduler.py``) - with a mocked
  planner (no Ollama), the guardian's reasons from cycle 1 must appear verbatim in cycle 2's
  digest, and cycle 2's plan must record ``corrects_plan_id`` pointing back at cycle 1.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.cache import hour_band, occupancy_bucket, outdoor_bin
from agent.digest import build_digest
from agent.feedback import FeedbackTracker
from agent.scheduler import Scheduler
from common.models import (
    Actuator,
    BuildingState,
    GuardianDecision,
    GuardianEvent,
    Plan,
    TriggerEnum,
    ZoneState,
)
from common.planslot import PlanSlot

# --------------------------------------------------------------------------------------
# Cache key discretization boundaries
# --------------------------------------------------------------------------------------


def test_hour_band_boundaries() -> None:
    # night | morning | midday | peak | night, per agent/cache.py's own thresholds.
    assert hour_band(0) == "night"
    assert hour_band(5) == "night"
    assert hour_band(6) == "morning"
    assert hour_band(9) == "morning"
    assert hour_band(10) == "midday"
    assert hour_band(15) == "midday"
    assert hour_band(16) == "peak"
    assert hour_band(21) == "peak"
    assert hour_band(22) == "night"
    assert hour_band(23) == "night"


def _state_with_occupants(total: float) -> BuildingState:
    return BuildingState(
        sim_time=datetime(2017, 7, 15, 12, 0),
        outdoor_air_temp_c=30.0,
        facility_power_w=1000.0,
        zones=[ZoneState(zone="Z1", air_temp_c=24.0, occupancy=total)],
    )


def test_occupancy_bucket_boundaries() -> None:
    assert occupancy_bucket(_state_with_occupants(0.0)) == "empty"
    assert occupancy_bucket(_state_with_occupants(0.01)) == "partial"
    assert occupancy_bucket(_state_with_occupants(8.0)) == "partial"
    assert occupancy_bucket(_state_with_occupants(8.01)) == "full"


def test_outdoor_bin_snaps_to_the_nearest_2c_and_rounds_half_up() -> None:
    assert outdoor_bin(30.0) == 30
    assert outdoor_bin(30.9) == 30
    assert outdoor_bin(31.0) == 32  # banker's-rounding edge: round(15.5) -> 16 in Python 3
    assert outdoor_bin(31.1) == 32
    assert outdoor_bin(-1.0) == 0  # round-half-to-even at the negative boundary; no raise


# --------------------------------------------------------------------------------------
# Guardian rate-history across restarts
# --------------------------------------------------------------------------------------


def test_rate_limit_falls_back_to_observed_setpoint_after_a_restart() -> None:
    """A lost in-memory RateHistory (the exact state after a process restart) must not let the
    next plan jump further than the rate policy allows - the *observed* setpoint becomes the
    anchor, so the very first post-restart cycle is rate-limited exactly like any other."""
    from common.models import PlanStep, SetpointPlan
    from guardian.core import Guardian, RateHistory
    from guardian.limits import DEFAULT_CONFIG

    guardian = Guardian()
    state = ZoneState(
        zone="Z1", air_temp_c=24.0, occupancy=5.0, cooling_setpoint_c=24.0, heating_setpoint_c=21.0
    )
    fresh_history = RateHistory.empty()  # exactly what a restarted process starts with

    hostile_jump = SetpointPlan(
        planner_model="test",
        steps=[
            PlanStep(
                offset_minutes=0, zone="Z1", actuator=Actuator.COOLING_SETPOINT_C, value=30.0
            )
        ],
    )
    verdict = guardian.filter(hostile_jump, state, fresh_history)

    rate = DEFAULT_CONFIG.rate
    applied = verdict.safe_plan.steps[0].value
    assert abs(applied - state.cooling_setpoint_c) <= rate.max_step_per_timestep_c + 1e-9
    assert verdict.clipped


def test_rate_history_prunes_stale_samples_across_a_long_gap() -> None:
    """A restart is also just an unusually long gap between samples; the trailing-hour window
    must prune samples across it exactly as it would across a normal hour, not carry a sample
    from before the gap forward forever."""
    from guardian.core import RateHistory

    history = RateHistory.empty()
    t0 = datetime(2017, 7, 15, 9, 0)
    history = history.record("Z1", t0, 24.0)

    # A restart: nothing happens for six hours, then the process is live again.
    t1 = t0 + timedelta(hours=6)
    history = history.record("Z1", t1, 25.0)

    # The pre-restart sample is more than an hour old relative to the new sample - gone.
    assert history.oldest("Z1").at == t1
    assert history.last("Z1").value == 25.0


# --------------------------------------------------------------------------------------
# L2 feedback-injection, with a mocked planner (no Ollama)
# --------------------------------------------------------------------------------------


class _FakePlanner:
    """Records every digest it is handed; returns a canned, schema-legal Plan each call."""

    def __init__(self) -> None:
        self.digests: list[str] = []
        self._call = 0

    def plan(self, digest: str, *, now: datetime, trigger: TriggerEnum) -> Plan:
        self.digests.append(digest)
        self._call += 1
        return Plan(
            plan_id=f"mock-plan-{self._call}", created_at=now, trigger=trigger,
            planner_model="mock", actions=[],
        )


class _FakeStore:
    """Records every ``write_plan`` call - just the ``corrects_plan_id`` argument matters here."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def write_plan(self, plan: Plan, *, run_id: str | None = None, corrects_plan_id=None) -> None:
        self.calls.append({"plan_id": plan.plan_id, "corrects_plan_id": corrects_plan_id})


def test_l2_feedback_appears_in_cycle_two_digest_and_links_corrects_plan_id() -> None:
    planner = _FakePlanner()
    store = _FakeStore()
    feedback = FeedbackTracker()
    plan_slot = PlanSlot()
    # The in-memory buffer the real Executor exposes via events_snapshot() - a plain list here,
    # since only FeedbackTracker.observe()'s consumption of it is under test.
    guardian_events: list[GuardianEvent] = []

    state = _state_with_occupants(4.0)

    def digest_provider(now: datetime) -> str:
        feedback.observe(guardian_events)
        return build_digest(
            state, active_plan=plan_slot.get(), feedback=feedback.pending_feedback()
        )

    scheduler = Scheduler(
        planner=planner, plan_slot=plan_slot, digest_provider=digest_provider,
        baseline={}, store=store, run_id="test", feedback=feedback,
    )

    now1 = datetime(2017, 7, 15, 9, 0)
    scheduler._epoch = 1
    scheduler._run_cycle(now1, TriggerEnum.HOURLY, epoch=1, state=state)

    assert "PREVIOUS PLAN FEEDBACK:\n  (none)" in planner.digests[0]
    assert store.calls[0]["corrects_plan_id"] is None
    first_plan_id = store.calls[0]["plan_id"]

    # The guardian clamped cycle 1's plan - exactly what Executor.decide would journal.
    guardian_events.append(
        GuardianEvent(
            plan_id=first_plan_id,
            decision=GuardianDecision.CLAMPED,
            note="clip: Z1 24.9->24.5 envelope_max",
        )
    )

    now2 = datetime(2017, 7, 15, 10, 0)
    scheduler._epoch = 2
    scheduler._run_cycle(now2, TriggerEnum.HOURLY, epoch=2, state=state)

    assert "clip: Z1 24.9->24.5 envelope_max" in planner.digests[1], (
        "cycle 2's digest must carry cycle 1's guardian reasons verbatim"
    )
    assert store.calls[1]["corrects_plan_id"] == first_plan_id, (
        "cycle 2's plan must record which plan it corrects"
    )
