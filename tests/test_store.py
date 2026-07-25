"""Telemetry store: batching discipline (rule R3) and WAL concurrent-read behaviour.

The batching tests are the ones that matter. R3 is not a performance nicety - one INSERT per
timestep inside a synchronous C callback can stall the simulation - so "nothing hit the disk
before the threshold" is asserted directly against a second connection, not against an
internal counter that could lie.

State fixtures are built from :mod:`common.models` types, never from hand-rolled dicts
(rule R4).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from common.models import BuildingState, ZoneState
from common.store import (
    TelemetryStore,
    connect,
    init_db,
    read_llm_calls,
    read_plans,
    read_telemetry,
)

ZONES = ("Core_ZN", "Perimeter_ZN_1")


def make_state(step: int, *, base_temp: float = 24.0) -> BuildingState:
    """One synthetic observation, ``step`` timesteps after midnight."""
    when = datetime(2017, 7, 15) + timedelta(minutes=10 * step)
    return BuildingState(
        sim_time=when,
        outdoor_air_temp_c=30.0 + step * 0.1,
        facility_power_w=1000.0 + step,
        zones=[
            ZoneState(
                zone=zone,
                air_temp_c=base_temp + index * 0.5,
                occupancy=float(index),
                cooling_setpoint_c=24.0,
                heating_setpoint_c=21.0,
                pmv=0.1 * index,
            )
            for index, zone in enumerate(ZONES)
        ],
    )


def row_count(db_path: Path, table: str = "telemetry") -> int:
    """Count rows through an independent connection - what actually reached the file."""
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "hive.sqlite"


# --------------------------------------------------------------------------------------
# WAL configuration
# --------------------------------------------------------------------------------------


def test_journal_mode_is_wal_and_persists(db_path: Path) -> None:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_synchronous_is_normal(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        # 1 == NORMAL
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# Batching (rule R3)
# --------------------------------------------------------------------------------------


def test_nothing_is_written_before_the_flush_threshold(db_path: Path) -> None:
    with TelemetryStore(db_path, flush_every_timesteps=12) as store:
        for step in range(11):
            store.record_timestep("run-a", make_state(step))

        assert store.buffered_rows == 11 * len(ZONES)
        assert row_count(db_path) == 0, "R3 violated: rows hit the disk before the threshold"


def test_flush_happens_exactly_on_the_threshold(db_path: Path) -> None:
    with TelemetryStore(db_path, flush_every_timesteps=12) as store:
        for step in range(11):
            store.record_timestep("run-a", make_state(step))
        assert row_count(db_path) == 0

        store.record_timestep("run-a", make_state(11))  # 12th timestep
        assert row_count(db_path) == 12 * len(ZONES)
        assert store.buffered_rows == 0


def test_close_flushes_the_partial_batch(db_path: Path) -> None:
    with TelemetryStore(db_path, flush_every_timesteps=12) as store:
        for step in range(5):
            store.record_timestep("run-a", make_state(step))
        assert row_count(db_path) == 0

    # Context manager exit must not lose the tail.
    assert row_count(db_path) == 5 * len(ZONES)


def test_batching_is_one_insert_per_batch_not_per_timestep(db_path: Path) -> None:
    """Guards the actual R3 invariant: a batch is a single executemany, not N executes."""
    with TelemetryStore(db_path, flush_every_timesteps=4) as store:
        for step in range(4):
            store.record_timestep("run-a", make_state(step))
        assert store.rows_written == 4 * len(ZONES)


def test_plan_commit_flushes_telemetry_first(db_path: Path) -> None:
    from common.models import ApprovedPlan, GuardianDecision

    with TelemetryStore(db_path, flush_every_timesteps=100) as store:
        for step in range(3):
            store.record_timestep("run-a", make_state(step))
        assert row_count(db_path) == 0

        store.write_approved_plan(
            ApprovedPlan(plan_id="p1", decision=GuardianDecision.ACCEPTED), run_id="run-a"
        )
        # Telemetry must land before (or with) the plan that it justified.
        assert row_count(db_path) == 3 * len(ZONES)
        assert row_count(db_path, "plans") == 1


def test_invalid_flush_interval_is_rejected(db_path: Path) -> None:
    with pytest.raises(ValueError):
        TelemetryStore(db_path, flush_every_timesteps=0)


# --------------------------------------------------------------------------------------
# WAL concurrent read
# --------------------------------------------------------------------------------------


def test_reader_sees_committed_rows_while_writer_stays_open(db_path: Path) -> None:
    """The dashboard's exact access pattern: read a live run without blocking the writer."""
    with TelemetryStore(db_path, flush_every_timesteps=2) as store:
        for step in range(4):
            store.record_timestep("run-a", make_state(step))
        # 4 timesteps at a threshold of 2 => two flushes committed.

        frame = read_telemetry(db_path, run_id="run-a")
        assert len(frame) == 4 * len(ZONES)
        assert set(frame["zone"]) == set(ZONES)

        # Writer is still usable afterwards - the reader did not lock it out.
        store.record_timestep("run-a", make_state(4))
        store.record_timestep("run-a", make_state(5))
        assert row_count(db_path) == 6 * len(ZONES)


def test_reader_does_not_see_buffered_rows(db_path: Path) -> None:
    """Buffered-but-unflushed telemetry is invisible to readers - that is the trade R3 makes."""
    with TelemetryStore(db_path, flush_every_timesteps=100) as store:
        for step in range(7):
            store.record_timestep("run-a", make_state(step))
        assert read_telemetry(db_path).empty

        store.flush()
        assert len(read_telemetry(db_path)) == 7 * len(ZONES)


def test_reader_connection_is_query_only(db_path: Path) -> None:
    from common.store import reader

    init_db(db_path)
    with reader(db_path) as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO runs (run_id, started_at) VALUES ('x', 'y')")


def test_concurrent_readers_and_writer_coexist(db_path: Path) -> None:
    """Two open readers plus an active writer - WAL's whole reason for being here."""
    from common.store import reader

    with TelemetryStore(db_path, flush_every_timesteps=1) as store:
        store.record_timestep("run-a", make_state(0))
        with reader(db_path) as first, reader(db_path) as second:
            assert first.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == len(ZONES)
            store.record_timestep("run-a", make_state(1))  # writer proceeds while readers hold
            assert second.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] >= len(ZONES)
        assert row_count(db_path) == 2 * len(ZONES)


# --------------------------------------------------------------------------------------
# Journal tables
# --------------------------------------------------------------------------------------


def test_plans_table_holds_both_stages(db_path: Path) -> None:
    from common.models import ApprovedPlan, GuardianDecision, Plan

    with TelemetryStore(db_path) as store:
        plan = Plan(plan_id="p1", planner_model="test-model")
        store.write_plan(plan, run_id="run-a")
        store.write_approved_plan(
            ApprovedPlan(plan_id="p1", decision=GuardianDecision.CLAMPED), run_id="run-a"
        )

    frame = read_plans(db_path, run_id="run-a")
    assert set(frame["stage"]) == {"proposed", "approved"}
    assert len(frame) == 2


def test_llm_calls_records_failures(db_path: Path) -> None:
    with TelemetryStore(db_path) as store:
        store.write_llm_call(
            model="qwen2.5", latency_ms=1234.0, ok=False, error="timeout", run_id="run-a"
        )

    frame = read_llm_calls(db_path, run_id="run-a")
    assert len(frame) == 1
    assert int(frame.iloc[0]["ok"]) == 0
    assert frame.iloc[0]["error"] == "timeout"


def test_telemetry_roundtrips_pmv_and_setpoints(db_path: Path) -> None:
    with TelemetryStore(db_path, flush_every_timesteps=1) as store:
        store.record_timestep("run-a", make_state(0), facility_kwh_step=0.25)

    frame = read_telemetry(db_path, run_id="run-a")
    core = frame[frame["zone"] == "Core_ZN"].iloc[0]
    assert core["cooling_setpoint_c"] == pytest.approx(24.0)
    assert core["heating_setpoint_c"] == pytest.approx(21.0)
    assert core["pmv"] == pytest.approx(0.0)
    assert core["facility_kwh_step"] == pytest.approx(0.25)
