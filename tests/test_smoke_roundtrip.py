"""The smoke harness's verdict logic, tested without EnergyPlus.

The harness itself needs a real simulation; this covers the part that decides pass/fail, so a
green smoke run cannot be green for the wrong reason - in particular, it must not pass when
the setpoint never moved, or when the building did not respond.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from common.models import PreparedModel, ZoneBinding
from experiments.smoke_roundtrip import build_dumb_plan, evaluate_roundtrip


def frame(*, temps: dict[int, float], setpoints: dict[int, float]) -> pd.DataFrame:
    """One day of hourly telemetry for a single zone."""
    start = datetime(2017, 7, 15)
    rows = []
    for hour in range(24):
        rows.append(
            {
                "sim_time": start + timedelta(hours=hour),
                "zone": "Core_ZN",
                "air_temp_c": temps.get(hour, 24.0),
                "cooling_setpoint_c": setpoints.get(hour, 24.0),
            }
        )
    return pd.DataFrame(rows)


def control_frame() -> pd.DataFrame:
    return frame(temps={}, setpoints={})


def responding_agent_frame() -> pd.DataFrame:
    # Setpoint +2 and temperature +0.8 during 14:00-16:00.
    return frame(
        temps={14: 24.8, 15: 24.8},
        setpoints={14: 26.0, 15: 26.0},
    )


def test_closed_loop_passes() -> None:
    result = evaluate_roundtrip(control_frame(), responding_agent_frame())
    assert result.setpoint_moved
    assert result.building_responded
    assert result.ok
    assert result.temp_delta_c == pytest.approx(0.8, abs=1e-6)
    assert result.setpoint_delta_c == pytest.approx(2.0, abs=1e-6)


def test_fails_when_setpoint_never_moved() -> None:
    """Temperature drifting up on its own must not be mistaken for a working actuator."""
    agent = frame(temps={14: 24.8, 15: 24.8}, setpoints={})
    result = evaluate_roundtrip(control_frame(), agent)
    assert not result.setpoint_moved
    assert not result.ok


def test_fails_when_building_did_not_respond() -> None:
    """Writing to an inert handle moves the setpoint report but changes no physics."""
    agent = frame(temps={}, setpoints={14: 26.0, 15: 26.0})
    result = evaluate_roundtrip(control_frame(), agent)
    assert result.setpoint_moved
    assert not result.building_responded
    assert not result.ok


def test_fails_when_response_is_backwards() -> None:
    """Raising a cooling setpoint must not make the zone colder."""
    agent = frame(temps={14: 23.0, 15: 23.0}, setpoints={14: 26.0, 15: 26.0})
    result = evaluate_roundtrip(control_frame(), agent)
    assert result.temp_delta_c < 0
    assert not result.ok


def test_empty_telemetry_fails_rather_than_passing_vacuously() -> None:
    empty = pd.DataFrame(columns=["sim_time", "zone", "air_temp_c", "cooling_setpoint_c"])
    result = evaluate_roundtrip(empty, empty)
    assert result.samples == 0
    assert not result.ok


def test_only_the_window_is_compared() -> None:
    """Movement outside 14:00-16:00 must not count toward the verdict."""
    agent = frame(temps={9: 30.0, 20: 30.0}, setpoints={9: 28.0, 20: 28.0})
    result = evaluate_roundtrip(control_frame(), agent)
    assert result.samples == 2  # 14:00 and 15:00 only
    assert not result.ok


def test_describe_reports_the_verdict() -> None:
    text = evaluate_roundtrip(control_frame(), responding_agent_frame()).describe()
    assert "PASS" in text
    assert "setpoint delta" in text


# --------------------------------------------------------------------------------------
# The dumb plan itself
# --------------------------------------------------------------------------------------


def model() -> PreparedModel:
    return PreparedModel(
        idf_path="agentic.idf",
        zones=[
            ZoneBinding(zone="Core_ZN", cooling_schedule="CLGSETP", heating_schedule="HTGSETP"),
            ZoneBinding(zone="Perimeter_ZN_1", cooling_schedule="CLGSETP2"),
        ],
        constant_schedules={"CLGSETP": 24.0, "HTGSETP": 21.0, "CLGSETP2": 24.0},
    )


def test_dumb_plan_sets_back_then_restores() -> None:
    plan = build_dumb_plan(model(), 24.0)
    core = [s for s in plan.steps if s.zone == "Core_ZN"]
    assert len(core) == 2

    setback, restore = sorted(core, key=lambda s: s.offset_minutes)
    assert setback.offset_minutes == 14 * 60
    assert setback.value == pytest.approx(26.0)
    assert restore.offset_minutes == 16 * 60
    assert restore.value == pytest.approx(24.0), "must return to baseline, not stay setback"


def test_dumb_plan_skips_zones_without_a_cooling_schedule() -> None:
    prepared = PreparedModel(
        idf_path="agentic.idf",
        zones=[ZoneBinding(zone="NoCooling_ZN")],
        constant_schedules={},
    )
    assert build_dumb_plan(prepared, 24.0).steps == []


def test_dumb_plan_covers_every_cooled_zone() -> None:
    plan = build_dumb_plan(model(), 24.0)
    assert {s.zone for s in plan.steps} == {"Core_ZN", "Perimeter_ZN_1"}
