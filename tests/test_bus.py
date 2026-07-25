"""The bus's handle lifecycle and callback contract, driven by a fake exchange.

These are the H6 tripwire behaviours. Every one of them fails *silently* against the real
runtime API - a handle fetched too early returns -1 and then reads as a plausible 0.0 forever,
a callback that raises kills the run with an unusable traceback - so they are asserted here
against a stand-in that mimics the API's shape and its unhelpfulness.

What this does not cover: that the real EnergyPlus honours the writes. That needs a real
install and is what ``experiments/smoke_roundtrip.py`` exists to prove.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent.bus import (
    VAR_COOLING_SETPOINT,
    VAR_HEATING_SETPOINT,
    VAR_OCCUPANCY,
    VAR_OUTDOOR_TEMP,
    VAR_PMV,
    VAR_ZONE_AIR_TEMP,
    SimulationBus,
)
from common.models import (
    Actuator,
    ApprovedPlan,
    GuardianDecision,
    PlanStep,
    PreparedModel,
    ZoneBinding,
)
from common.store import TelemetryStore, read_telemetry

ZONE = "Core_ZN"
PEOPLE = "Core_ZN People"  # deliberately different from the zone name - that is the point
COOLING_SCHEDULE = "CLGSETP_SCH"
HEATING_SCHEDULE = "HTGSETP_SCH"

STATE = object()  # opaque handle, exactly as the real API treats it


def make_model() -> PreparedModel:
    return PreparedModel(
        idf_path="agentic.idf",
        zones=[
            ZoneBinding(
                zone=ZONE,
                cooling_schedule=COOLING_SCHEDULE,
                heating_schedule=HEATING_SCHEDULE,
                people=PEOPLE,
            )
        ],
        constant_schedules={COOLING_SCHEDULE: 24.0, HEATING_SCHEDULE: 21.0},
    )


class FakeExchange:
    """Mimics ``pyenergyplus.api.exchange``, including returning -1 rather than raising."""

    def __init__(self, *, ready: bool = True, warmup: bool = False) -> None:
        self.ready = ready
        self.warmup = warmup
        self.requested: list[tuple[str, str]] = []
        self.variable_handle_lookups = 0
        self.actuator_handle_lookups = 0
        self.writes: list[tuple[str, float]] = []
        self.raise_on_read = False

        self.values: dict[tuple[str, str], float] = {
            (VAR_ZONE_AIR_TEMP, ZONE): 24.5,
            (VAR_OCCUPANCY, ZONE): 3.0,
            (VAR_COOLING_SETPOINT, ZONE): 24.0,
            (VAR_HEATING_SETPOINT, ZONE): 21.0,
            (VAR_PMV, PEOPLE): 0.35,
            (VAR_OUTDOOR_TEMP, "Environment"): 33.0,
        }
        self._var_handles: dict[int, tuple[str, str]] = {}
        self._act_handles: dict[int, str] = {}
        self._next_handle = 1
        self.meter_joules = 3_600_000.0  # 1 kWh

        self.year_v, self.month_v, self.day_v = 2017, 7, 15
        self.hour_v, self.minute_v = 0, 10

    # -- lifecycle gates ---------------------------------------------------------------

    def request_variable(self, state, name: str, key: str) -> None:
        self.requested.append((name, key))

    def api_data_fully_ready(self, state) -> bool:
        return self.ready

    def warmup_flag(self, state) -> int:
        return 1 if self.warmup else 0

    # -- handles -----------------------------------------------------------------------

    def get_variable_handle(self, state, name: str, key: str) -> int:
        self.variable_handle_lookups += 1
        if (name, key) not in self.values:
            return -1
        handle = self._next_handle
        self._next_handle += 1
        self._var_handles[handle] = (name, key)
        return handle

    def get_variable_value(self, state, handle: int) -> float:
        if self.raise_on_read:
            raise RuntimeError("simulated exchange failure")
        return self.values[self._var_handles[handle]]

    def get_meter_handle(self, state, name: str) -> int:
        handle = self._next_handle
        self._next_handle += 1
        return handle

    def get_meter_value(self, state, handle: int) -> float:
        return self.meter_joules

    def get_actuator_handle(self, state, component: str, control: str, key: str) -> int:
        self.actuator_handle_lookups += 1
        assert component == "Schedule:Constant"
        assert control == "Schedule Value"
        handle = self._next_handle
        self._next_handle += 1
        self._act_handles[handle] = key
        return handle

    def set_actuator_value(self, state, handle: int, value: float) -> None:
        self.writes.append((self._act_handles[handle], value))

    # -- clock -------------------------------------------------------------------------

    def year(self, state) -> int:
        return self.year_v

    def month(self, state) -> int:
        return self.month_v

    def day_of_month(self, state) -> int:
        return self.day_v

    def hour(self, state) -> int:
        return self.hour_v

    def minutes(self, state) -> int:
        return self.minute_v

    def zone_time_step(self, state) -> float:
        return 1.0 / 6.0


class FakeApi:
    def __init__(self, exchange: FakeExchange) -> None:
        self.exchange = exchange


@pytest.fixture
def bus_and_exchange(tmp_path: Path):
    exchange = FakeExchange()
    store = TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1)
    bus = SimulationBus(
        model=make_model(),
        store=store,
        run_id="test-run",
        epw_path=tmp_path / "weather.epw",
        out_dir=tmp_path / "out",
    )
    bus._api = FakeApi(exchange)
    yield bus, exchange, store
    store.close()


# --------------------------------------------------------------------------------------
# (1) request_variable before the run
# --------------------------------------------------------------------------------------


def test_pmv_is_requested_by_people_name_not_zone_name() -> None:
    """The single most common way to get a -1 PMV handle."""
    bus = SimulationBus(
        model=make_model(),
        store=None,  # not touched by _requested_variables
        run_id="r",
        epw_path="w.epw",
        out_dir="out",
    )
    pairs = bus._requested_variables()

    assert (VAR_PMV, PEOPLE) in pairs
    assert (VAR_PMV, ZONE) not in pairs, "PMV keyed by zone would return -1"
    assert (VAR_ZONE_AIR_TEMP, ZONE) in pairs
    assert (VAR_OUTDOOR_TEMP, "Environment") in pairs


def test_every_read_variable_is_in_the_request_list() -> None:
    """Anything read but not requested is a guaranteed -1 at runtime."""
    bus = SimulationBus(
        model=make_model(), store=None, run_id="r", epw_path="w.epw", out_dir="out"
    )
    requested = set(bus._requested_variables())
    for variable in (VAR_ZONE_AIR_TEMP, VAR_OCCUPANCY, VAR_COOLING_SETPOINT, VAR_HEATING_SETPOINT):
        assert (variable, ZONE) in requested


# --------------------------------------------------------------------------------------
# (2) api_data_fully_ready gate, (3) handle caching
# --------------------------------------------------------------------------------------


def test_no_handles_are_fetched_before_data_is_ready(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    exchange.ready = False

    for _ in range(5):
        bus._on_timestep(STATE)

    assert exchange.variable_handle_lookups == 0
    assert exchange.actuator_handle_lookups == 0
    assert bus.stats.not_ready_skips == 5
    assert bus.stats.timesteps == 0


def test_handles_are_fetched_exactly_once(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange

    for step in range(10):
        exchange.minute_v = 10
        exchange.hour_v = step
        bus._on_timestep(STATE)

    expected = len(bus._requested_variables())
    assert exchange.variable_handle_lookups == expected, "handles re-fetched per timestep"
    assert exchange.actuator_handle_lookups == 2
    assert bus.stats.timesteps == 10


# --------------------------------------------------------------------------------------
# (3) warmup guard
# --------------------------------------------------------------------------------------


def test_warmup_timesteps_are_skipped_entirely(bus_and_exchange) -> None:
    bus, exchange, store = bus_and_exchange
    exchange.warmup = True

    for _ in range(4):
        bus._on_timestep(STATE)

    assert bus.stats.warmup_skips == 4
    assert bus.stats.timesteps == 0
    store.flush()
    assert read_telemetry(store.db_path).empty, "warmup values must not enter telemetry"


def test_read_state_returns_none_during_warmup(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)  # prime handles
    exchange.warmup = True
    assert bus.read_state(STATE) is None


def test_write_setpoints_is_a_noop_during_warmup(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    exchange.warmup = True
    exchange.writes.clear()

    approved = ApprovedPlan(
        plan_id="p1",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=0, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=26.0
            )
        ],
    )
    assert bus.write_setpoints(STATE, approved, now=datetime(2017, 7, 15, 14)) == 0
    assert exchange.writes == []


# --------------------------------------------------------------------------------------
# (4) the callback never raises
# --------------------------------------------------------------------------------------


def test_callback_swallows_exceptions_and_falls_back(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)  # prime handles successfully
    exchange.raise_on_read = True
    exchange.writes.clear()

    bus._on_timestep(STATE)  # must not raise

    assert bus.stats.callback_errors == 1
    assert bus.stats.fallback_actuations == 1
    assert dict(exchange.writes) == {COOLING_SCHEDULE: 24.0, HEATING_SCHEDULE: 21.0}


def test_callback_survives_a_failure_before_handles_exist(tmp_path: Path) -> None:
    """An exception on the very first callback must still not cross the C boundary."""
    exchange = FakeExchange()
    exchange.raise_on_read = True
    store = TelemetryStore(tmp_path / "hive.sqlite", flush_every_timesteps=1)
    bus = SimulationBus(
        model=make_model(),
        store=store,
        run_id="r",
        epw_path=tmp_path / "w.epw",
        out_dir=tmp_path / "out",
    )
    bus._api = FakeApi(exchange)

    bus._on_timestep(STATE)  # must not raise

    assert bus.stats.callback_errors == 1
    store.close()


def test_simulation_continues_after_repeated_failures(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    exchange.raise_on_read = True

    for _ in range(20):
        bus._on_timestep(STATE)

    assert bus.stats.callback_errors == 20  # every one contained


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def test_read_state_populates_zone_and_facility_channels(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)

    observation = bus.read_state(STATE)
    zone = observation.zones[0]

    assert zone.zone == ZONE
    assert zone.air_temp_c == pytest.approx(24.5)
    assert zone.pmv == pytest.approx(0.35)
    assert zone.occupancy == pytest.approx(3.0)
    assert zone.cooling_setpoint_c == pytest.approx(24.0)
    assert observation.outdoor_air_temp_c == pytest.approx(33.0)
    # 1 kWh over a 10-minute step = 6 kW.
    assert observation.facility_power_w == pytest.approx(6000.0)


def test_zone_without_an_air_temp_handle_is_omitted(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    del exchange.values[(VAR_ZONE_AIR_TEMP, ZONE)]

    bus._on_timestep(STATE)

    assert f"{VAR_ZONE_AIR_TEMP}[{ZONE}]" in bus.stats.missing_handles
    assert bus.read_state(STATE).zones == []


def test_minute_60_rolls_over_to_the_next_hour(bus_and_exchange) -> None:
    """E+ reports hour 0-23 and minutes 1-60; minute 60 means the top of the next hour."""
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)

    exchange.hour_v, exchange.minute_v = 13, 60
    assert bus.sim_datetime(STATE) == datetime(2017, 7, 15, 14, 0)

    exchange.hour_v, exchange.minute_v = 23, 60
    assert bus.sim_datetime(STATE) == datetime(2017, 7, 16, 0, 0)


def test_sim_datetime_is_none_before_the_calendar_is_meaningful(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    exchange.month_v = 0
    assert bus.sim_datetime(STATE) is None


def test_telemetry_lands_in_the_store(bus_and_exchange) -> None:
    bus, exchange, store = bus_and_exchange
    for hour in range(3):
        exchange.hour_v = hour
        bus._on_timestep(STATE)
    store.flush()

    frame = read_telemetry(store.db_path, run_id="test-run")
    assert len(frame) == 3
    assert frame["pmv"].notna().all()


# --------------------------------------------------------------------------------------
# Writing: plan resolution
# --------------------------------------------------------------------------------------


def approved_setback() -> ApprovedPlan:
    """Baseline at t0, setback at +840 min (14:00), restore at +960 min (16:00)."""
    return ApprovedPlan(
        plan_id="p1",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=0, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=24.0
            ),
            PlanStep(
                offset_minutes=840, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=26.0
            ),
            PlanStep(
                offset_minutes=960, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=24.0
            ),
        ],
    )


def test_latest_applicable_step_wins(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    approved = approved_setback()
    anchor = datetime(2017, 7, 15, 0, 0)

    bus.write_setpoints(STATE, approved, now=anchor)  # anchors the plan here
    exchange.writes.clear()

    bus.write_setpoints(STATE, approved, now=anchor + timedelta(minutes=900))  # 15:00
    assert exchange.writes == [(COOLING_SCHEDULE, 26.0)]

    exchange.writes.clear()
    bus.write_setpoints(STATE, approved, now=anchor + timedelta(minutes=1000))  # 16:40
    assert exchange.writes == [(COOLING_SCHEDULE, 24.0)], "must restore after the window"


def test_future_steps_are_not_applied_early(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    approved = approved_setback()
    anchor = datetime(2017, 7, 15, 0, 0)

    bus.write_setpoints(STATE, approved, now=anchor)
    assert exchange.writes == [(COOLING_SCHEDULE, 24.0)], "only the t0 step is due"


def test_plan_is_anchored_in_simulation_time_not_wall_clock(bus_and_exchange) -> None:
    """``approved_at`` is wall-clock UTC; mixing it with sim time would misfire every step."""
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    approved = approved_setback()

    first_seen = datetime(2017, 7, 15, 0, 0)
    bus.write_setpoints(STATE, approved, now=first_seen)
    assert bus._plan_anchor[approved.plan_id] == first_seen

    # Anchor is sticky: a later call does not re-anchor and re-trigger the setback.
    bus.write_setpoints(STATE, approved, now=first_seen + timedelta(hours=20))
    assert bus._plan_anchor[approved.plan_id] == first_seen


def test_unwired_actuator_is_skipped_not_misapplied(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    exchange.writes.clear()

    approved = ApprovedPlan(
        plan_id="p2",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=0, zone=ZONE, actuator=Actuator.FAN_FLOW_FRACTION, value=0.5
            )
        ],
    )
    assert bus.write_setpoints(STATE, approved, now=datetime(2017, 7, 15)) == 0
    assert exchange.writes == []


def test_unknown_zone_in_an_approved_plan_is_skipped(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    exchange.writes.clear()

    approved = ApprovedPlan(
        plan_id="p3",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=0,
                zone="Ghost_ZN",
                actuator=Actuator.COOLING_SETPOINT_C,
                value=26.0,
            )
        ],
    )
    assert bus.write_setpoints(STATE, approved, now=datetime(2017, 7, 15)) == 0


def test_heating_and_cooling_write_to_different_schedules(bus_and_exchange) -> None:
    bus, exchange, _ = bus_and_exchange
    bus._on_timestep(STATE)
    exchange.writes.clear()

    approved = ApprovedPlan(
        plan_id="p4",
        decision=GuardianDecision.ACCEPTED,
        steps=[
            PlanStep(
                offset_minutes=0, zone=ZONE, actuator=Actuator.COOLING_SETPOINT_C, value=26.0
            ),
            PlanStep(
                offset_minutes=0, zone=ZONE, actuator=Actuator.HEATING_SETPOINT_C, value=20.0
            ),
        ],
    )
    bus.write_setpoints(STATE, approved, now=datetime(2017, 7, 15))
    assert dict(exchange.writes) == {COOLING_SCHEDULE: 26.0, HEATING_SCHEDULE: 20.0}
