"""Receding-horizon mode: chunk planning, profile rendering, and the drive loop.

The EnergyPlus run and the eppy write are injected, so the loop itself - stage a plan, render
it into the chunk, run, read back, advance - is fully covered without an install. That loop is
the contingency; if it only worked on a machine with EnergyPlus we would not know it worked
until we needed it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from common.models import (
    Actuator,
    ApprovedPlan,
    BuildingState,
    ControlInterface,
    GuardianDecision,
    PlanStep,
    PreparedModel,
    ZoneBinding,
    ZoneState,
)
from common.store import TelemetryStore, read_telemetry
from simulation.receding import (
    HorizonChunk,
    RecedingHorizonDriver,
    breakpoints_for_chunk,
    compact_fields,
    plan_chunks,
)

ZONE = "Core_ZN"
COOLING_SCHEDULE = "CLGSETP_SCH"
ANCHOR = datetime(2017, 7, 15, 0, 0)


def make_model() -> PreparedModel:
    return PreparedModel(
        idf_path="agentic.idf",
        zones=[ZoneBinding(zone=ZONE, cooling_schedule=COOLING_SCHEDULE)],
        constant_schedules={COOLING_SCHEDULE: 24.0},
    )


def setback_plan() -> ApprovedPlan:
    return ApprovedPlan(
        plan_id="p1",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=840, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=26.0
            ),
            PlanStep(
                offset_minutes=960, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=24.0
            ),
        ],
    )


# --------------------------------------------------------------------------------------
# Chunk planning
# --------------------------------------------------------------------------------------


def test_single_day_is_one_chunk() -> None:
    chunks = plan_chunks(date(2017, 7, 15), 1, 1)
    assert len(chunks) == 1
    assert chunks[0].start == chunks[0].end == date(2017, 7, 15)
    assert chunks[0].days == 1


def test_chunks_tile_the_run_without_gaps_or_overlap() -> None:
    chunks = plan_chunks(date(2017, 7, 1), 7, 2)
    assert [(c.start.day, c.end.day) for c in chunks] == [(1, 2), (3, 4), (5, 6), (7, 7)]
    assert sum(c.days for c in chunks) == 7


def test_chunks_are_indexed_in_order() -> None:
    chunks = plan_chunks(date(2017, 7, 1), 5, 2)
    assert [c.index for c in chunks] == [0, 1, 2]


def test_chunks_cross_month_boundaries() -> None:
    chunks = plan_chunks(date(2017, 7, 30), 4, 2)
    assert chunks[1].start == date(2017, 8, 1)


def test_invalid_spans_are_rejected() -> None:
    with pytest.raises(ValueError):
        plan_chunks(date(2017, 7, 1), 0, 1)
    with pytest.raises(ValueError):
        plan_chunks(date(2017, 7, 1), 1, 0)


# --------------------------------------------------------------------------------------
# Profile rendering
# --------------------------------------------------------------------------------------


def kwargs(**overrides):
    base = {
        "anchor": ANCHOR,
        "chunk_start": ANCHOR,
        "chunk_days": 1,
        "zone": ZONE,
        "actuator": Actuator.COOLING_SETPOINT_C,
        "baseline": 24.0,
    }
    base.update(overrides)
    return base


def test_setback_becomes_three_breakpoints() -> None:
    points = breakpoints_for_chunk(setback_plan(), **kwargs())
    assert points == [(0, 24.0), (840, 26.0), (960, 24.0)]


def test_value_in_force_before_the_chunk_carries_in() -> None:
    """A chunk starting mid-setback must begin at the setback value, not the baseline."""
    points = breakpoints_for_chunk(
        setback_plan(), **kwargs(chunk_start=ANCHOR + timedelta(minutes=900))
    )
    assert points[0] == (0, 26.0), "chunk starts inside the 14:00-16:00 window"
    assert points[1] == (60, 24.0), "restore lands 60 min into this chunk"


def test_plan_entirely_in_the_past_flattens_to_its_final_value() -> None:
    points = breakpoints_for_chunk(
        setback_plan(), **kwargs(chunk_start=ANCHOR + timedelta(days=2))
    )
    assert points == [(0, 24.0)]


def test_steps_beyond_the_horizon_are_excluded() -> None:
    """A step that lands past the end of this chunk waits for the next one.

    ``PlanStep.offset_minutes`` is capped at 1440 by the contract, so this only arises when the
    chunk began *before* the plan was anchored - which is exactly what happens when a plan
    arrives mid-run.
    """
    plan = ApprovedPlan(
        plan_id="p", decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=1400, zone=ZONE,
                actuator=Actuator.COOLING_SETPOINT_C, value=30.0,
            )
        ],
    )
    points = breakpoints_for_chunk(
        plan, **kwargs(chunk_start=ANCHOR - timedelta(minutes=120))
    )
    assert points == [(0, 24.0)], "step lands at minute 1520, past this chunk's 1440"


def test_other_zones_are_ignored() -> None:
    plan = ApprovedPlan(
        plan_id="p", decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=100, zone="Other_ZN",
                actuator=Actuator.COOLING_SETPOINT_C, value=30.0,
            )
        ],
    )
    assert breakpoints_for_chunk(plan, **kwargs()) == [(0, 24.0)]


def test_consecutive_duplicates_collapse() -> None:
    plan = ApprovedPlan(
        plan_id="p", decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=60, zone=ZONE,
                actuator=Actuator.COOLING_SETPOINT_C, value=24.0,
            )
        ],
    )
    assert breakpoints_for_chunk(plan, **kwargs()) == [(0, 24.0)]


def test_compact_fields_render_until_blocks() -> None:
    fields = compact_fields([(0, 24.0), (840, 26.0), (960, 24.0)], chunk_days=1)
    assert fields[0] == "Through: 12/31"
    assert fields[1] == "For: AllDays"
    assert "Until: 14:00" in fields
    assert "Until: 16:00" in fields
    assert fields[-1] == "24"  # trailing block runs to midnight at the baseline


def test_compact_fields_cover_the_full_day() -> None:
    fields = compact_fields([(0, 24.0)], chunk_days=1)
    assert fields == ["Through: 12/31", "For: AllDays", "Until: 24:00", "24"]


def test_compact_fields_reject_an_empty_profile() -> None:
    with pytest.raises(ValueError):
        compact_fields([], chunk_days=1)


# --------------------------------------------------------------------------------------
# The drive loop
# --------------------------------------------------------------------------------------


def observations_for(chunk_index: int, count: int = 3) -> list[BuildingState]:
    start = ANCHOR + timedelta(days=chunk_index)
    return [
        BuildingState(
            sim_time=start + timedelta(hours=index),
            outdoor_air_temp_c=33.0,
            facility_power_w=1000.0,
            zones=[ZoneState(zone=ZONE, air_temp_c=24.0 + index * 0.1, cooling_setpoint_c=24.0)],
        )
        for index in range(count)
    ]


class Recorder:
    """Captures what the loop asked the outside world to do."""

    def __init__(self, *, fail_at: int | None = None) -> None:
        self.written: list[Path] = []
        self.ran: list[Path] = []
        self.fail_at = fail_at

    def write(self, chunk: HorizonChunk, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"chunk {chunk.index}\n", encoding="utf-8")
        self.written.append(out_path)
        return out_path

    def run(self, idf_path: Path, epw_path: Path, out_dir: Path) -> int:
        self.ran.append(idf_path)
        if self.fail_at is not None and len(self.ran) - 1 == self.fail_at:
            return 1
        return 0

    def read(self, sql_path: Path, model: PreparedModel) -> list[BuildingState]:
        return observations_for(len(self.ran) - 1)


def make_driver(tmp_path: Path, store: TelemetryStore, recorder: Recorder, **overrides):
    kwargs_ = {
        "model": make_model(),
        "store": store,
        "run_id": "receding-test",
        "epw_path": tmp_path / "w.epw",
        "out_dir": tmp_path / "out",
        "start_date": date(2017, 7, 15),
        "total_days": 1,
        "horizon_days": 1,
        "runner": recorder.run,
        "reader": recorder.read,
        "chunk_writer": recorder.write,
    }
    kwargs_.update(overrides)
    return RecedingHorizonDriver(**kwargs_)


def test_one_day_completes_end_to_end(tmp_path: Path) -> None:
    """The acceptance criterion for receding mode."""
    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder)
        assert driver.run() == 0

    assert driver.stats.chunks_run == 1
    assert driver.stats.chunks_failed == 0
    assert driver.stats.timesteps_recorded == 3
    assert len(recorder.ran) == 1

    frame = read_telemetry(tmp_path / "hive.sqlite", run_id="receding-test")
    assert len(frame) == 3


def test_multi_day_advances_through_every_chunk(tmp_path: Path) -> None:
    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder, total_days=3)
        assert driver.run() == 0

    assert driver.stats.chunks_run == 3
    assert len(recorder.written) == 3
    assert driver.stats.timesteps_recorded == 9


def test_read_state_reflects_the_previous_chunk(tmp_path: Path) -> None:
    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder, total_days=2)
        assert driver.read_state() is None, "nothing observed before the first chunk"
        driver.run()

    last = driver.read_state()
    assert last is not None
    assert last.sim_time == ANCHOR + timedelta(days=1, hours=2)


def test_a_failed_chunk_stops_the_run(tmp_path: Path) -> None:
    recorder = Recorder(fail_at=1)
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder, total_days=3)
        assert driver.run() == 1

    assert driver.stats.chunks_run == 1
    assert driver.stats.chunks_failed == 1


def test_write_setpoints_stages_values_for_the_next_chunk(tmp_path: Path) -> None:
    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder)
        plan = setback_plan()
        # First sight anchors the plan in simulation time; only later calls have elapsed time
        # to measure against. Same anchoring rule as the live bus.
        driver.write_setpoints(plan, now=ANCHOR)
        assert driver._staged[COOLING_SCHEDULE] == pytest.approx(24.0)

        staged = driver.write_setpoints(plan, now=ANCHOR + timedelta(minutes=900))

    assert staged == 2
    assert driver._staged[COOLING_SCHEDULE] == pytest.approx(26.0)


def test_future_steps_are_not_staged_early(tmp_path: Path) -> None:
    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder)
        driver.write_setpoints(setback_plan(), now=ANCHOR)

    assert driver._staged[COOLING_SCHEDULE] == pytest.approx(24.0)


def test_plan_provider_is_consulted_each_chunk(tmp_path: Path) -> None:
    recorder = Recorder()
    seen: list[datetime] = []

    def provider(now: datetime) -> ApprovedPlan:
        seen.append(now)
        return setback_plan()

    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder, total_days=3, plan_provider=provider)
        driver.run()

    assert len(seen) == 3
    assert seen[0] == ANCHOR  # first chunk has no prior observation


def test_snapshots_are_committed_per_chunk(tmp_path: Path) -> None:
    from simulation.snapshots import SnapshotWriter

    def materialize(base_idf, control_state, out_path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("stub\n", encoding="utf-8")

    recorder = Recorder()
    writer = SnapshotWriter(
        base_idf=tmp_path / "agentic.idf",
        versions_dir=tmp_path / "versions",
        materializer=materialize,
    )
    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(tmp_path, store, recorder, total_days=2, snapshot_writer=writer)
        driver.run()

    # Two chunks, identical control state (no plan) -> deduped to a single version.
    assert writer.version_count == 1
    assert driver.stats.snapshots == 1


def test_changing_setpoints_produce_distinct_versions(tmp_path: Path) -> None:
    from simulation.snapshots import SnapshotWriter

    def materialize(base_idf, control_state, out_path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("stub\n", encoding="utf-8")

    recorder = Recorder()
    writer = SnapshotWriter(
        base_idf=tmp_path / "agentic.idf",
        versions_dir=tmp_path / "versions",
        materializer=materialize,
    )

    # A step one hour in: not yet due when chunk 0 is staged, due by the time chunk 1 is.
    early = ApprovedPlan(
        plan_id="p-early",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=60, zone=ZONE,
                actuator=Actuator.COOLING_SETPOINT_C, value=26.0,
            )
        ],
    )

    def provider(now: datetime) -> ApprovedPlan:
        return early

    with TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1) as store:
        driver = make_driver(
            tmp_path, store, recorder, total_days=2,
            snapshot_writer=writer, plan_provider=provider,
        )
        driver.run()

    # v1 = baseline going into chunk 0; v2 = setback in force going into chunk 1.
    assert writer.version_count == 2
    assert writer.head.state.schedule_values[COOLING_SCHEDULE] == pytest.approx(26.0)


# --------------------------------------------------------------------------------------
# Interface parity
# --------------------------------------------------------------------------------------


def test_driver_satisfies_the_control_interface(tmp_path: Path) -> None:
    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite") as store:
        driver = make_driver(tmp_path, store, recorder)
        assert isinstance(driver, ControlInterface)


def test_live_bus_satisfies_the_same_interface(tmp_path: Path) -> None:
    """Both modes must be substitutable, or the contingency is not a contingency."""
    from agent.bus import SimulationBus

    with TelemetryStore(tmp_path / "hive.sqlite") as store:
        bus = SimulationBus(
            model=make_model(),
            store=store,
            run_id="r",
            epw_path=tmp_path / "w.epw",
            out_dir=tmp_path / "out",
        )
        assert isinstance(bus, ControlInterface)


def test_both_modes_accept_the_same_call_shape(tmp_path: Path) -> None:
    """Agent-shaped code, run against both. If this compiles for one it must for the other."""
    from agent.bus import SimulationBus

    def agent_code(controller: ControlInterface) -> int:
        state = controller.read_state()
        del state
        return controller.write_setpoints(setback_plan(), now=ANCHOR)

    recorder = Recorder()
    with TelemetryStore(tmp_path / "hive.sqlite") as store:
        driver = make_driver(tmp_path, store, recorder)
        assert agent_code(driver) == 2

        bus = SimulationBus(
            model=make_model(), store=store, run_id="r",
            epw_path=tmp_path / "w.epw", out_dir=tmp_path / "out",
        )
        # No callback in flight, so the bus has no state handle and writes nothing - but the
        # call shape is identical, which is the property under test.
        assert agent_code(bus) == 0
