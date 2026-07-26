"""The sensor -> actuator round trip over the ``pyenergyplus`` runtime API.

This is the critical path, and the runtime API punishes small lifecycle mistakes with silent
wrong answers rather than errors. The four rules this class exists to enforce:

1. **``request_variable()`` for every output variable, before ``run_energyplus()``.**
   Variables not requested before the run simply do not exist in the exchange. Asking for their
   handle later returns ``-1`` - not an exception, not a message, just ``-1``, which then reads
   as a plausible-looking ``0.0`` forever if you do not check.

2. **No handle lookups until ``api_data_fully_ready(state)``.** Handles fetched earlier are
   garbage. Once ready, handles are stable for the whole run, so they are fetched exactly once
   and cached - re-looking them up per timestep is both slow and pointless.

3. **Every read and write guarded on ``warmup_flag(state)``.** Callbacks fire during warmup
   days. Warmup values are not physical and warmup writes are discarded, so acting on them
   corrupts telemetry and wastes actuation.

4. **The callback is synchronous C. Its entire body is wrapped in ``try/except``.** An
   exception propagating across the C boundary kills the simulation with an unusable
   traceback. On any exception we log, actuate the baseline fallback, and return normally
   (CLAUDE.md rule R1). No asyncio, no network, no blocking I/O other than the batched
   telemetry flush.

**On rule R2**: :meth:`write_setpoints` takes an :class:`~common.models.ApprovedPlan`, never a
raw ``Plan`` or a bare ``PlanStep``. That is deliberate and load-bearing - the type system is
what makes "no bypass path" checkable. The only producer of ``ApprovedPlan`` is the guardian.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from common import eplus_path
from common.log import get_logger
from common.models import (
    Actuator,
    AppliedControlState,
    ApprovedPlan,
    BuildingState,
    PreparedModel,
    ZoneState,
)
from common.store import TelemetryStore

log = get_logger("agent.bus")

J_PER_KWH = 3_600_000.0

# Output variables the bus reads, and how each one's key is derived.
VAR_ZONE_AIR_TEMP = "Zone Mean Air Temperature"
VAR_OCCUPANCY = "Zone People Occupant Count"
VAR_COOLING_SETPOINT = "Zone Thermostat Cooling Setpoint Temperature"
VAR_HEATING_SETPOINT = "Zone Thermostat Heating Setpoint Temperature"
VAR_PMV = "Zone Thermal Comfort Fanger Model PMV"
VAR_OUTDOOR_TEMP = "Site Outdoor Air Drybulb Temperature"
SITE_KEY = "Environment"
FACILITY_METER = "Electricity:Facility"
#: Some prototype IDFs (observed: the DOE small-office reference building under EnergyPlus
#: 24.1) never populate a runtime-API handle for the aggregate Facility meter, even though it
#: reports fine in the SQL output - the live callback and the SQL-based KPI extractor
#: (`experiments/kpis.py`) go through entirely different EnergyPlus subsystems, so this only
#: affects the live path. `Electricity:Building` (interior end-uses) is the closest available
#: substitute for a live facility-power reading when Facility itself has no handle.
_FACILITY_METER_FALLBACK = "Electricity:Building"

#: Which PlanStep actuators the bus can currently drive, and the ZoneBinding field holding the
#: Schedule:Constant name for each. Anything else is logged once and skipped rather than
#: silently dropped - a plan asking for an unwired actuator is a real finding.
SCHEDULE_ACTUATORS: dict[Actuator, str] = {
    Actuator.COOLING_SETPOINT_C: "cooling_schedule",
    Actuator.HEATING_SETPOINT_C: "heating_schedule",
}

_ACTUATOR_COMPONENT = "Schedule:Constant"
_ACTUATOR_CONTROL = "Schedule Value"
_BAD_HANDLE = -1


@dataclass
class BusStats:
    """What happened during a run. The error count is the number that matters."""

    timesteps: int = 0
    warmup_skips: int = 0
    not_ready_skips: int = 0
    actuator_writes: int = 0
    callback_errors: int = 0
    fallback_actuations: int = 0
    missing_handles: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"timesteps={self.timesteps} writes={self.actuator_writes} "
            f"errors={self.callback_errors} fallbacks={self.fallback_actuations} "
            f"warmup_skips={self.warmup_skips} missing_handles={len(self.missing_handles)}"
        )


PlanProvider = Callable[[datetime], ApprovedPlan | None]


class SimulationBus:
    """Drives one EnergyPlus run, reading state and writing guardian-approved setpoints.

    ``plan_provider`` is called from inside the callback, so it must be non-blocking and
    cheap: a dictionary lookup against an already-approved plan. It must never call an LLM.
    In production that is the plan cache; in the smoke harness it is a constant function.
    """

    def __init__(
        self,
        *,
        model: PreparedModel,
        store: TelemetryStore,
        run_id: str,
        epw_path: Path | str,
        out_dir: Path | str,
        idf_path: Path | str | None = None,
        plan_provider: PlanProvider | None = None,
        flush_every_timesteps: int = 12,
    ) -> None:
        self.model = model
        self.store = store
        self.run_id = run_id
        self.epw_path = Path(epw_path)
        self.out_dir = Path(out_dir)
        self.idf_path = Path(idf_path or model.idf_path)
        self.plan_provider = plan_provider
        self.flush_every_timesteps = max(1, flush_every_timesteps)
        self.stats = BusStats()

        self._api = None
        self._state = None
        #: Set at the top of every callback so the ControlInterface methods can be called
        #: without threading the opaque EnergyPlus state handle through agent code.
        self._current_state = None
        self._handles_ready = False
        self._var_handles: dict[tuple[str, str], int] = {}
        self._meter_handle: int = _BAD_HANDLE
        self._actuator_handles: dict[str, int] = {}
        self._plan_anchor: dict[str, datetime] = {}
        self._warned: set[str] = set()

        #: Values currently written to each schedule, seeded with the prepared baseline.
        self._applied: dict[str, float] = dict(model.constant_schedules)
        #: Every *distinct* control state this run passed through, in order.
        #:
        #: Snapshots are NOT materialised here. Writing an IDF means an eppy load and save -
        #: hundreds of milliseconds of blocking I/O, inside a synchronous C callback, which is
        #: exactly what rule R1 forbids. So the callback only appends a small record when the
        #: applied values actually change (a dict compare over a handful of floats), and
        #: `simulation.snapshots` materialises the series after the run.
        self.control_history: list[AppliedControlState] = []

    # -- variable inventory ------------------------------------------------------------

    def _requested_variables(self) -> list[tuple[str, str]]:
        """Every ``(variable_name, key)`` pair this bus will ever ask for.

        Built from the :class:`PreparedModel` index rather than guessed, which is what makes
        the PMV key correct: PMV is keyed by People object name, the rest by zone name.
        """
        pairs: list[tuple[str, str]] = [(VAR_OUTDOOR_TEMP, SITE_KEY)]
        for binding in self.model.zones:
            for variable in (
                VAR_ZONE_AIR_TEMP,
                VAR_OCCUPANCY,
                VAR_COOLING_SETPOINT,
                VAR_HEATING_SETPOINT,
            ):
                pairs.append((variable, binding.zone))
            if binding.people:
                pairs.append((VAR_PMV, binding.people))
        return pairs

    # -- lifecycle ---------------------------------------------------------------------

    def run(self, *, extra_args: list[str] | None = None) -> int:
        """Run EnergyPlus to completion. Returns the exit code.

        Ordering here is the whole point of the class: request variables, register the
        callback, *then* run. Nothing touches a handle before the callback fires.
        """
        eplus_path.require_energyplus()
        from pyenergyplus.api import EnergyPlusAPI

        if not self.idf_path.is_file():
            raise FileNotFoundError(
                f"{self.idf_path} not found. Run `python -m simulation.prepare_idf` first."
            )
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self._api = EnergyPlusAPI()
        self._state = self._api.state_manager.new_state()

        # (1) Request every variable BEFORE the run. A handle for an unrequested variable is
        #     -1, with no error anywhere.
        for variable, key in self._requested_variables():
            self._api.exchange.request_variable(self._state, variable, key)

        self._api.runtime.callback_end_zone_timestep_after_zone_reporting(
            self._state, self._on_timestep
        )

        self.store.start_run(self.run_id, label=self.idf_path.name)
        args = ["-w", str(self.epw_path), "-d", str(self.out_dir), *(extra_args or [])]
        args.append(str(self.idf_path))

        try:
            exit_code = self._api.runtime.run_energyplus(self._state, args)
        finally:
            # Flush before tearing down: the last partial batch is real data.
            try:
                self.store.flush()
                self.store.end_run(self.run_id)
            finally:
                self._api.state_manager.delete_state(self._state)
                self._state = None

        log.info("run %s finished exit=%s %s", self.run_id, exit_code, self.stats.summary())
        if self.stats.callback_errors:
            log.warning(
                "%d callback error(s) were swallowed to keep the simulation alive - "
                "telemetry for those timesteps is missing",
                self.stats.callback_errors,
            )
        return exit_code

    # -- the callback ------------------------------------------------------------------

    def _on_timestep(self, state) -> None:
        """Synchronous C callback. Must never raise, never block, never await."""
        try:
            self._current_state = state

            # (2) No handles, no reads, nothing until the exchange is populated.
            if not self._api.exchange.api_data_fully_ready(state):
                self.stats.not_ready_skips += 1
                return

            # (3) Warmup days are not physical. Skip them entirely.
            if self._api.exchange.warmup_flag(state):
                self.stats.warmup_skips += 1
                return

            if not self._handles_ready:
                self._fetch_handles(state)
                if not self._handles_ready:
                    return

            observation = self.read_state(state)
            if observation is None:
                return

            kwh = self._facility_kwh_step(state)
            self.store.record_timestep(self.run_id, observation, facility_kwh_step=kwh)

            if self.plan_provider is not None:
                approved = self.plan_provider(observation.sim_time)
                if approved is not None:
                    self.write_setpoints(approved, now=observation.sim_time, state=state)

            self.stats.timesteps += 1

        except Exception:
            # (4) Nothing escapes to the C boundary. Log, fall back, carry on.
            self.stats.callback_errors += 1
            log.exception("callback failed at timestep %d - falling back", self.stats.timesteps)
            self._actuate_baseline(state)

    # -- reading -----------------------------------------------------------------------

    def _fetch_handles(self, state) -> None:
        """Fetch and cache every handle. Called once, after ``api_data_fully_ready``."""
        exchange = self._api.exchange

        for variable, key in self._requested_variables():
            handle = exchange.get_variable_handle(state, variable, key)
            self._var_handles[(variable, key)] = handle
            if handle == _BAD_HANDLE:
                self.stats.missing_handles.append(f"{variable}[{key}]")

        self._meter_handle = exchange.get_meter_handle(state, FACILITY_METER)
        if self._meter_handle == _BAD_HANDLE:
            self._meter_handle = exchange.get_meter_handle(state, _FACILITY_METER_FALLBACK)
            if self._meter_handle == _BAD_HANDLE:
                self.stats.missing_handles.append(f"meter[{FACILITY_METER}]")
            else:
                log.warning(
                    "meter[%s] unavailable; falling back to meter[%s] for live facility power",
                    FACILITY_METER,
                    _FACILITY_METER_FALLBACK,
                )

        for schedule in sorted(self.model.constant_schedules):
            handle = exchange.get_actuator_handle(
                state, _ACTUATOR_COMPONENT, _ACTUATOR_CONTROL, schedule
            )
            self._actuator_handles[schedule] = handle
            if handle == _BAD_HANDLE:
                self.stats.missing_handles.append(f"actuator[{schedule}]")

        self._handles_ready = True
        if self.stats.missing_handles:
            log.warning(
                "%d handle(s) unavailable: %s",
                len(self.stats.missing_handles),
                ", ".join(self.stats.missing_handles[:8]),
            )
        log.info(
            "handles cached: %d variables, %d actuators, meter=%s",
            len(self._var_handles),
            len(self._actuator_handles),
            self._meter_handle,
        )

    def _value(self, state, variable: str, key: str) -> float | None:
        """Read one variable, or ``None`` if its handle is unavailable."""
        handle = self._var_handles.get((variable, key), _BAD_HANDLE)
        if handle == _BAD_HANDLE:
            return None
        return float(self._api.exchange.get_variable_value(state, handle))

    def sim_datetime(self, state) -> datetime | None:
        """Current simulation timestamp, or ``None`` before the calendar is meaningful.

        EnergyPlus reports ``hour`` as 0-23 and ``minutes`` as 1-60, timestamping the *end* of
        the interval - so minute 60 means the top of the next hour. Building the value with a
        ``timedelta`` from midnight makes that roll over correctly instead of raising.
        """
        exchange = self._api.exchange
        month = exchange.month(state)
        day = exchange.day_of_month(state)
        if not month or not day:
            return None
        year = exchange.year(state) or 2017
        hour = exchange.hour(state)
        minute = exchange.minutes(state)
        return datetime(int(year), int(month), int(day)) + timedelta(
            hours=int(hour), minutes=int(minute)
        )

    def _step_hours(self, state) -> float:
        """Length of the current zone timestep, in hours."""
        try:
            value = float(self._api.exchange.zone_time_step(state))
        except Exception:
            return 1.0 / 6.0
        return value if value > 0 else 1.0 / 6.0

    def _facility_kwh_step(self, state) -> float | None:
        """Facility electricity for this timestep, in kWh. Meters report joules."""
        if self._meter_handle == _BAD_HANDLE:
            return None
        joules = float(self._api.exchange.get_meter_value(state, self._meter_handle))
        return joules / J_PER_KWH

    def read_state(self, state: object | None = None) -> BuildingState | None:
        """Build the current observation. Satisfies :class:`~common.models.ControlInterface`.

        Returns a :class:`BuildingState` - the whole-building observation, of which per-zone
        :class:`ZoneState` records are a part. Callers get temperature, PMV, occupancy and
        both setpoints per zone, plus facility power and the simulation timestamp.

        ``state`` is the opaque EnergyPlus handle. Callers inside the callback pass it; agent
        code written against the mode-agnostic interface omits it and gets the handle from the
        callback currently in flight.

        ``None`` means "not observable yet": warmup, or the calendar not yet meaningful.
        """
        state = state if state is not None else self._current_state
        if state is None:
            return None
        if self._api.exchange.warmup_flag(state):
            return None
        now = self.sim_datetime(state)
        if now is None:
            return None

        zones: list[ZoneState] = []
        for binding in self.model.zones:
            air_temp = self._value(state, VAR_ZONE_AIR_TEMP, binding.zone)
            if air_temp is None:
                self._warn_once(
                    f"no-air-temp:{binding.zone}",
                    "zone %s has no air-temperature handle; omitted from state",
                    binding.zone,
                )
                continue
            pmv = self._value(state, VAR_PMV, binding.people) if binding.people else None
            zones.append(
                ZoneState(
                    zone=binding.zone,
                    air_temp_c=air_temp,
                    occupancy=self._non_negative(
                        self._value(state, VAR_OCCUPANCY, binding.zone)
                    ),
                    cooling_setpoint_c=self._value(state, VAR_COOLING_SETPOINT, binding.zone),
                    heating_setpoint_c=self._value(state, VAR_HEATING_SETPOINT, binding.zone),
                    pmv=pmv,
                )
            )

        outdoor = self._value(state, VAR_OUTDOOR_TEMP, SITE_KEY)
        kwh = self._facility_kwh_step(state)
        step_hours = self._step_hours(state)
        power_w = (kwh * 1000.0 / step_hours) if (kwh is not None and step_hours > 0) else 0.0

        return BuildingState(
            sim_time=now,
            outdoor_air_temp_c=outdoor if outdoor is not None else 0.0,
            facility_power_w=max(0.0, power_w),
            zones=zones,
        )

    @staticmethod
    def _non_negative(value: float | None) -> float | None:
        """Occupancy is constrained ``ge=0`` in the contract; clamp sensor noise below zero."""
        if value is None:
            return None
        return max(0.0, value)

    # -- writing -----------------------------------------------------------------------

    def write_setpoints(
        self, approved: ApprovedPlan, *, now: datetime, state: object | None = None
    ) -> int:
        """Actuate a **guardian-approved** plan. Returns the number of actuator writes.

        Deliberately typed to ``ApprovedPlan``, not ``Plan`` or ``PlanStep`` (rule R2): the
        only way to reach an actuator is through an object the guardian constructed.

        Step offsets are relative to when the plan was issued, but ``approved_at`` is
        wall-clock UTC while the simulation runs in simulation time - mixing them silently
        produces nonsense. So the bus anchors each plan to the *simulation* time at which it
        first saw that ``plan_id`` and measures offsets from there.

        Signature matches :class:`~common.models.ControlInterface` so the receding-horizon
        driver is a drop-in replacement.
        """
        state = state if state is not None else self._current_state
        if state is None or self._api.exchange.warmup_flag(state):
            return 0

        anchor = self._plan_anchor.setdefault(approved.plan_id, now)
        elapsed_minutes = (now - anchor).total_seconds() / 60.0

        # Latest applicable step wins, per (zone, actuator).
        effective: dict[tuple[str, Actuator], tuple[int, float]] = {}
        for step in approved.steps:
            if step.offset_minutes > elapsed_minutes:
                continue
            key = (step.zone, step.actuator)
            previous = effective.get(key)
            if previous is None or step.offset_minutes >= previous[0]:
                effective[key] = (step.offset_minutes, step.value)

        writes = 0
        for (zone, actuator), (_, value) in effective.items():
            schedule = self._schedule_for(zone, actuator)
            if schedule is None:
                continue
            handle = self._actuator_handles.get(schedule, _BAD_HANDLE)
            if handle == _BAD_HANDLE:
                self._warn_once(
                    f"no-actuator:{schedule}",
                    "no actuator handle for schedule %s; cannot write %s",
                    schedule,
                    actuator,
                )
                continue
            self._api.exchange.set_actuator_value(state, handle, float(value))
            self._applied[schedule] = float(value)
            writes += 1

        self.stats.actuator_writes += writes
        self._note_control_state(now, plan_id=approved.plan_id, trigger="plan_commit")
        return writes

    def _note_control_state(self, now: datetime, *, plan_id: str | None, trigger: str) -> None:
        """Append to :attr:`control_history` iff the applied values actually changed.

        Cheap by construction - a hash over a few floats, no I/O - because this runs on the
        callback path (rule R1). Materialisation happens after the run.
        """
        state = AppliedControlState(
            sim_time=now,
            schedule_values=dict(self._applied),
            plan_id=plan_id,
            trigger=trigger,
        )
        if self.control_history and self.control_history[-1].content_hash() == state.content_hash():
            return
        self.control_history.append(state)

    def _schedule_for(self, zone: str, actuator: Actuator) -> str | None:
        """Resolve ``(zone, actuator)`` to the Schedule:Constant name that drives it."""
        attribute = SCHEDULE_ACTUATORS.get(actuator)
        if attribute is None:
            self._warn_once(
                f"unwired:{actuator}",
                "actuator %s is not wired to a schedule in this model; step ignored",
                actuator,
            )
            return None
        binding = self.model.binding(zone)
        if binding is None:
            self._warn_once(f"unknown-zone:{zone}", "plan references unknown zone %s", zone)
            return None
        schedule = getattr(binding, attribute, None)
        if not schedule:
            self._warn_once(
                f"no-schedule:{zone}:{attribute}",
                "zone %s has no %s; step ignored",
                zone,
                attribute,
            )
            return None
        return str(schedule)

    def _actuate_baseline(self, state) -> None:
        """Restore every schedule to the value ``prepare_idf`` initialised it to.

        The fallback of last resort, called from the callback's exception handler. It must not
        raise - a failure here would re-enter the C boundary, which is exactly what rule R1
        forbids.
        """
        try:
            if not self._handles_ready:
                return
            for schedule, baseline in self.model.constant_schedules.items():
                handle = self._actuator_handles.get(schedule, _BAD_HANDLE)
                if handle != _BAD_HANDLE:
                    self._api.exchange.set_actuator_value(state, handle, float(baseline))
                    self._applied[schedule] = float(baseline)
            self.stats.fallback_actuations += 1
            # A fallback is a real control change and belongs in the version series - it is
            # how "the agent stopped driving at 14:20" becomes visible in the deliverable.
            now = self.sim_datetime(state)
            if now is not None:
                self._note_control_state(now, plan_id=None, trigger="fallback")
        except Exception:
            log.exception("baseline fallback actuation failed; simulation continues uncontrolled")

    # -- helpers -----------------------------------------------------------------------

    def _warn_once(self, key: str, message: str, *args) -> None:
        """Log a warning the first time only - this runs at timestep frequency."""
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(message, *args)


__all__ = [
    "FACILITY_METER",
    "SCHEDULE_ACTUATORS",
    "VAR_COOLING_SETPOINT",
    "VAR_HEATING_SETPOINT",
    "VAR_OCCUPANCY",
    "VAR_OUTDOOR_TEMP",
    "VAR_PMV",
    "VAR_ZONE_AIR_TEMP",
    "BusStats",
    "PlanProvider",
    "SimulationBus",
]
