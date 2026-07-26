"""Receding-horizon actuation - the H6 contingency.

The live path (:class:`agent.bus.SimulationBus`) writes actuator values into a running
simulation through the runtime API. If that path fails us - a prototype with no writable
actuator, a platform where the callback misbehaves, an EnergyPlus version that moves the
handles - we lose the entire demo. This module is the insurance, and it is built now because
building it later, under pressure, is how contingencies end up not existing.

The trade instead of live actuation:

1. Bake the next horizon chunk's schedules into a copy of the model (eppy).
2. Simulate that chunk.
3. Read the results back out of the chunk's SQL output.
4. Advance and repeat, planning the next chunk from what just happened.

It is slower - one EnergyPlus start-up per chunk - and it cannot react *within* a chunk. What
it buys is that it depends on nothing but "EnergyPlus can run an IDF", which is the one thing
that cannot break.

**The interface is identical.** This class satisfies
:class:`~common.models.ControlInterface`, exactly like the live bus: ``read_state()`` and
``write_setpoints(approved, now=...)``. Agent code cannot tell which mode it is in, so
switching is a construction-site change, not a code change.

**Intra-chunk fidelity.** EnergyPlus ``RunPeriod`` is day-granular, so a chunk is at minimum one
day. Flattening a plan to one constant per chunk would throw away the whole point of a
time-varying setback, so the plan is instead rendered as a ``Schedule:Compact`` with ``Until:``
blocks for the chunk. The profile survives; only cross-chunk reaction latency is lost.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from common.log import get_logger
from common.models import (
    Actuator,
    AppliedControlState,
    ApprovedPlan,
    BuildingState,
    PreparedModel,
)
from common.store import TelemetryStore

log = get_logger("simulation.receding")

MINUTES_PER_DAY = 24 * 60

#: ``(idf_path, epw_path, out_dir) -> exit_code``
ChunkRunner = Callable[[Path, Path, Path], int]
#: ``(sql_path, model) -> observations``
ChunkReader = Callable[[Path, PreparedModel], list[BuildingState]]
#: ``(chunk, out_path) -> written_path``. The eppy step; injectable so the loop is testable.
ChunkWriter = Callable[["HorizonChunk", Path], Path]

SCHEDULE_ACTUATORS: dict[Actuator, str] = {
    Actuator.COOLING_SETPOINT_C: "cooling_schedule",
    Actuator.HEATING_SETPOINT_C: "heating_schedule",
}


# --------------------------------------------------------------------------------------
# Chunk planning (pure)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HorizonChunk:
    """One re-simulated slice of the run."""

    index: int
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def start_datetime(self) -> datetime:
        return datetime(self.start.year, self.start.month, self.start.day)


def plan_chunks(start: date, total_days: int, horizon_days: int) -> list[HorizonChunk]:
    """Split a run into consecutive day-aligned chunks. The last one may be short."""
    if total_days < 1:
        raise ValueError(f"total_days must be >= 1, got {total_days}")
    if horizon_days < 1:
        raise ValueError(f"horizon_days must be >= 1, got {horizon_days}")

    chunks: list[HorizonChunk] = []
    offset = 0
    while offset < total_days:
        span = min(horizon_days, total_days - offset)
        chunk_start = start + timedelta(days=offset)
        chunks.append(
            HorizonChunk(
                index=len(chunks),
                start=chunk_start,
                end=chunk_start + timedelta(days=span - 1),
            )
        )
        offset += span
    return chunks


def breakpoints_for_chunk(
    approved: ApprovedPlan,
    *,
    anchor: datetime,
    chunk_start: datetime,
    chunk_days: int,
    zone: str,
    actuator: Actuator,
    baseline: float,
) -> list[tuple[int, float]]:
    """Plan values for one schedule over one chunk, as ``(minute_of_chunk, value)``.

    Minutes are measured from the chunk's midnight. The value in force at minute 0 is whatever
    the plan says as of the chunk start - which may come from a step that fired days earlier,
    so the search looks backwards through every step, not just the ones inside the chunk.
    """
    horizon_minutes = chunk_days * MINUTES_PER_DAY
    offset_at_chunk_start = int((chunk_start - anchor).total_seconds() // 60)

    relevant = sorted(
        (step for step in approved.steps if step.zone == zone and step.actuator == actuator),
        key=lambda step: step.offset_minutes,
    )

    current = baseline
    points: list[tuple[int, float]] = []
    for step in relevant:
        minute = step.offset_minutes - offset_at_chunk_start
        if minute <= 0:
            current = step.value  # already in force before this chunk began
        elif minute < horizon_minutes:
            points.append((minute, step.value))

    merged = [(0, current), *points]

    # Collapse consecutive duplicates - a flat schedule should be one block, not five.
    collapsed: list[tuple[int, float]] = []
    for minute, value in merged:
        if collapsed and round(collapsed[-1][1], 4) == round(value, 4):
            continue
        collapsed.append((minute, value))
    return collapsed


def compact_fields(
    breakpoints: list[tuple[int, float]], *, chunk_days: int
) -> list[str]:
    """Render breakpoints as ``Schedule:Compact`` field values.

    One ``Through`` / ``For: AllDays`` block covering the chunk, then ``Until:`` pairs. Hours
    past 24 wrap onto the following day of the same block, so multi-day chunks repeat the daily
    profile - which is the right behaviour for a plan expressed in day-relative terms.
    """
    if not breakpoints:
        raise ValueError("need at least one breakpoint")

    fields = ["Through: 12/31", "For: AllDays"]
    ordered = sorted(breakpoints)

    for index, (minute, value) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else MINUTES_PER_DAY
        end = min(end, MINUTES_PER_DAY)
        if end <= minute:
            continue
        fields.append(f"Until: {end // 60:02d}:{end % 60:02d}")
        fields.append(f"{value:.4g}")

    # Guarantee the day is covered even if every block was degenerate.
    if len(fields) == 2:
        fields.extend(["Until: 24:00", f"{ordered[-1][1]:.4g}"])

    del chunk_days  # profile repeats daily within the chunk; RunPeriod bounds the span
    return fields


# --------------------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------------------


@dataclass
class RecedingStats:
    chunks_run: int = 0
    chunks_failed: int = 0
    timesteps_recorded: int = 0
    schedules_written: int = 0
    snapshots: int = 0
    missing_results: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"chunks={self.chunks_run} failed={self.chunks_failed} "
            f"timesteps={self.timesteps_recorded} schedules={self.schedules_written} "
            f"snapshots={self.snapshots}"
        )


class RecedingHorizonDriver:
    """Re-simulation actuation behind the live bus's interface.

    Satisfies :class:`~common.models.ControlInterface`. ``write_setpoints`` *stages* values for
    the next chunk rather than writing them into a running simulation; ``read_state`` returns
    the last observation parsed out of the previous chunk's results.
    """

    def __init__(
        self,
        *,
        model: PreparedModel,
        store: TelemetryStore,
        run_id: str,
        epw_path: Path | str,
        out_dir: Path | str,
        start_date: date,
        total_days: int = 1,
        horizon_days: int = 1,
        idf_path: Path | str | None = None,
        plan_provider: Callable[[datetime], ApprovedPlan | None] | None = None,
        snapshot_writer=None,
        install_dir: Path | str | None = None,
        runner: ChunkRunner | None = None,
        reader: ChunkReader | None = None,
        chunk_writer: ChunkWriter | None = None,
    ) -> None:
        self.model = model
        self.store = store
        self.run_id = run_id
        self.epw_path = Path(epw_path)
        self.out_dir = Path(out_dir)
        self.idf_path = Path(idf_path or model.idf_path)
        self.start_date = start_date
        self.total_days = total_days
        self.horizon_days = horizon_days
        self.plan_provider = plan_provider
        self.snapshot_writer = snapshot_writer
        self.install_dir = install_dir
        self._runner = runner or _run_energyplus
        self._reader = reader or read_chunk_results
        self._chunk_writer = chunk_writer or self._write_chunk_idf
        self.stats = RecedingStats()

        #: Schedule name -> value staged for the next chunk. Starts at the prepared baseline.
        self._staged: dict[str, float] = dict(model.constant_schedules)
        self._last_state: BuildingState | None = None
        self._plan_anchor: dict[str, datetime] = {}
        self._active_plan: ApprovedPlan | None = None
        self._warned: set[str] = set()

    # -- ControlInterface --------------------------------------------------------------

    def read_state(self, state: object | None = None) -> BuildingState | None:
        """Last observation from the previous chunk, or ``None`` before the first chunk ran."""
        del state  # no opaque handle in this mode; accepted for interface parity
        return self._last_state

    def write_setpoints(
        self, approved: ApprovedPlan, *, now: datetime, state: object | None = None
    ) -> int:
        """Stage a guardian-approved plan for the next chunk. Returns values staged.

        Same rule R2 contract as the live bus: only an ``ApprovedPlan`` gets here. The plan is
        retained so the chunk's ``Schedule:Compact`` can be rendered from its full profile
        rather than from a single flattened value.
        """
        del state
        self._plan_anchor.setdefault(approved.plan_id, now)
        self._active_plan = approved

        anchor = self._plan_anchor[approved.plan_id]
        elapsed_minutes = (now - anchor).total_seconds() / 60.0

        staged = 0
        for step in approved.steps:
            schedule = self._schedule_for(step.zone, step.actuator)
            if schedule is None:
                continue
            if step.offset_minutes <= elapsed_minutes:
                self._staged[schedule] = float(step.value)
            staged += 1

        self.stats.schedules_written += staged
        return staged

    def _schedule_for(self, zone: str, actuator: Actuator) -> str | None:
        attribute = SCHEDULE_ACTUATORS.get(actuator)
        if attribute is None:
            self._warn_once(f"unwired:{actuator}", "actuator %s not wired; ignored", actuator)
            return None
        binding = self.model.binding(zone)
        if binding is None:
            self._warn_once(f"zone:{zone}", "unknown zone %s in plan; ignored", zone)
            return None
        schedule = getattr(binding, attribute, None)
        return str(schedule) if schedule else None

    # -- the loop ----------------------------------------------------------------------

    def run(self) -> int:
        """Run every chunk in sequence. Returns 0 if all chunks completed."""
        chunks = plan_chunks(self.start_date, self.total_days, self.horizon_days)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.store.start_run(self.run_id, label=f"receding:{self.idf_path.name}")

        log.info(
            "receding: %d chunk(s) of %d day(s) from %s",
            len(chunks),
            self.horizon_days,
            self.start_date,
        )

        try:
            for chunk in chunks:
                self._plan_for(chunk)
                self._snapshot(chunk)
                if not self._run_chunk(chunk):
                    self.stats.chunks_failed += 1
                    break
                self.stats.chunks_run += 1
        finally:
            self.store.flush()
            self.store.end_run(self.run_id)

        log.info("receding run %s finished: %s", self.run_id, self.stats.summary())
        return 1 if self.stats.chunks_failed else 0

    def _plan_for(self, chunk: HorizonChunk) -> None:
        """Ask the provider for a plan covering this chunk and stage it."""
        if self.plan_provider is None:
            return
        now = self._last_state.sim_time if self._last_state else chunk.start_datetime
        approved = self.plan_provider(now)
        if approved is not None:
            self.write_setpoints(approved, now=now)

    def _snapshot(self, chunk: HorizonChunk) -> None:
        """Record the control state going *into* this chunk - that is the model being run."""
        if self.snapshot_writer is None:
            return
        state = AppliedControlState(
            sim_time=chunk.start_datetime,
            schedule_values=dict(self._staged),
            plan_id=self._active_plan.plan_id if self._active_plan else None,
            trigger=f"receding_chunk_{chunk.index}",
        )
        if self.snapshot_writer.commit(state) is not None:
            self.stats.snapshots += 1

    def _run_chunk(self, chunk: HorizonChunk) -> bool:
        chunk_dir = self.out_dir / f"chunk_{chunk.index:03d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_idf = chunk_dir / "chunk.idf"

        self._chunk_writer(chunk, chunk_idf)

        exit_code = self._runner(chunk_idf, self.epw_path, chunk_dir)
        if exit_code != 0:
            log.error(
                "chunk %d failed with exit code %s (see %s)", chunk.index, exit_code, chunk_dir
            )
            return False

        sql_path = chunk_dir / "eplusout.sql"
        observations = self._reader(sql_path, self.model)
        if not observations:
            self.stats.missing_results.append(str(sql_path))
            log.warning("chunk %d produced no readable results at %s", chunk.index, sql_path)
            return False

        for observation in observations:
            self.store.record_timestep(self.run_id, observation)
        self.stats.timesteps_recorded += len(observations)
        self._last_state = observations[-1]
        log.info(
            "chunk %d: %d timesteps, last=%s",
            chunk.index,
            len(observations),
            self._last_state.sim_time,
        )
        return True

    def _write_chunk_idf(self, chunk: HorizonChunk, out_path: Path) -> Path:
        """Materialise this chunk's model: RunPeriod + the staged schedule profile."""
        from simulation.idf_io import load_idf, save_idf

        idf = load_idf(self.idf_path, self.install_dir)
        _set_run_period(idf, chunk)
        self._write_schedules(idf, chunk)
        return save_idf(idf, out_path)

    def _write_schedules(self, idf, chunk: HorizonChunk) -> None:
        """Replace each controlled Schedule:Constant with a Compact profile for the chunk.

        Deduplicated by schedule **name**, not by zone: several zones can share one schedule
        (the DOE small-office prototype's whole-building setpoint schedule is exactly this -
        all five zones reference the same `CLGSETP_SCH`/`HTGSETP_SCH`), and an eppy IDF object
        is unique by name. Looping per zone and writing a `SCHEDULE:COMPACT` for each one that
        references a shared schedule produced literal duplicate-named objects - EnergyPlus
        rejects the model outright ("Duplicate name found... Fatal"). Whichever zone is first
        in sorted order stands in for the schedule when resolving the plan's steps, the same
        "one shared value, last write observed wins" reality the live bus already has for a
        schedule several zones point at - just made deterministic here since object order in a
        materialised file matters.
        """
        plan = self._active_plan
        constants = {
            str(s.Name).upper(): s for s in idf.idfobjects.get("SCHEDULE:CONSTANT", [])
        }

        schedule_zone: dict[tuple[Actuator, str], str] = {}
        for binding in sorted(self.model.zones, key=lambda b: b.zone):
            for actuator, attribute in SCHEDULE_ACTUATORS.items():
                name = getattr(binding, attribute, None)
                if name:
                    schedule_zone.setdefault((actuator, name), binding.zone)

        for (actuator, name), zone in schedule_zone.items():
            baseline = self._staged.get(
                name, self.model.constant_schedules.get(name, 24.0)
            )
            if plan is None:
                points = [(0, baseline)]
            else:
                anchor = self._plan_anchor.get(plan.plan_id, chunk.start_datetime)
                points = breakpoints_for_chunk(
                    plan,
                    anchor=anchor,
                    chunk_start=chunk.start_datetime,
                    chunk_days=chunk.days,
                    zone=zone,
                    actuator=actuator,
                    baseline=self.model.constant_schedules.get(name, baseline),
                )

            existing = constants.get(name.upper())
            limits = ""
            if existing is not None:
                limits = str(getattr(existing, "Schedule_Type_Limits_Name", "") or "")
                idf.removeidfobject(existing)
                constants.pop(name.upper(), None)

            new = idf.newidfobject("SCHEDULE:COMPACT", Name=name)
            if limits and "Schedule_Type_Limits_Name" in new.fieldnames:
                new.Schedule_Type_Limits_Name = limits
            for index, value in enumerate(compact_fields(points, chunk_days=chunk.days), 1):
                field_name = f"Field_{index}"
                if field_name in new.fieldnames:
                    setattr(new, field_name, value)

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        log.warning(message, *args)


# --------------------------------------------------------------------------------------
# Defaults that need EnergyPlus
# --------------------------------------------------------------------------------------


def _set_run_period(idf, chunk: HorizonChunk) -> None:
    run_periods = idf.idfobjects.get("RUNPERIOD", [])
    period = run_periods[0] if run_periods else idf.newidfobject("RUNPERIOD")
    period.Name = f"chunk_{chunk.index}"
    period.Begin_Month = chunk.start.month
    period.Begin_Day_of_Month = chunk.start.day
    period.End_Month = chunk.end.month
    period.End_Day_of_Month = chunk.end.day


def _run_energyplus(idf_path: Path, epw_path: Path, out_dir: Path) -> int:
    """Run one chunk. No callbacks: receding mode deliberately uses none of the exchange."""
    from common import eplus_path

    eplus_path.require_energyplus()
    from pyenergyplus.api import EnergyPlusAPI

    api = EnergyPlusAPI()
    state = api.state_manager.new_state()
    try:
        return api.runtime.run_energyplus(
            state, ["-w", str(epw_path), "-d", str(out_dir), str(idf_path)]
        )
    finally:
        api.state_manager.delete_state(state)


def read_chunk_results(sql_path: Path, model: PreparedModel) -> list[BuildingState]:
    """Reconstruct per-timestep observations from a chunk's ``eplusout.sql``.

    The live bus gets these from the exchange; here they come out of the SQL afterwards. Same
    :class:`BuildingState` contract either way, which is what keeps the two modes
    interchangeable downstream.

    **Must exclude the two HVAC-sizing design-day environments.** Every EnergyPlus run
    (``SimulationControl`` requires sizing) reports both design days into the same SQL as the
    actual RunPeriod chunk; without this filter their rows (a fixed, unrelated Jan/Jul design
    condition) get mixed into the chunk's observations, corrupting exactly the state the next
    chunk's plan is built from. Same filter ``experiments.kpis``/``experiments.report`` already
    apply to their own SQL reads - kept consistent rather than reinvented here.
    """
    import sqlite3

    from agent.bus import (
        VAR_COOLING_SETPOINT,
        VAR_HEATING_SETPOINT,
        VAR_OCCUPANCY,
        VAR_OUTDOOR_TEMP,
        VAR_PMV,
        VAR_ZONE_AIR_TEMP,
    )
    from common.models import ZoneState
    from experiments.kpis import _run_period_env_indices

    if not Path(sql_path).is_file():
        return []

    wanted = {
        VAR_ZONE_AIR_TEMP,
        VAR_OCCUPANCY,
        VAR_COOLING_SETPOINT,
        VAR_HEATING_SETPOINT,
        VAR_PMV,
        VAR_OUTDOOR_TEMP,
    }
    placeholders = ",".join("?" for _ in wanted)

    conn = sqlite3.connect(str(sql_path))
    conn.row_factory = sqlite3.Row
    try:
        env_indices = _run_period_env_indices(conn)
        rows = conn.execute(
            f"""
            SELECT rdd.Name AS variable, rdd.KeyValue AS key, rd.Value AS value,
                   t.Year AS year, t.Month AS month, t.Day AS day,
                   t.Hour AS hour, t.Minute AS minute, t.EnvironmentPeriodIndex AS env_idx
            FROM ReportData rd
            JOIN ReportDataDictionary rdd
                 ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
            JOIN Time t ON rd.TimeIndex = t.TimeIndex
            WHERE rdd.Name IN ({placeholders})
              AND (t.WarmupFlag = 0 OR t.WarmupFlag IS NULL)
            """,
            tuple(wanted),
        ).fetchall()
        if env_indices is not None:
            rows = [r for r in rows if r["env_idx"] is None or int(r["env_idx"]) in env_indices]
    finally:
        conn.close()

    samples: dict[datetime, dict[tuple[str, str], float]] = {}
    for row in rows:
        if not row["month"] or not row["day"]:
            continue
        stamp = datetime(int(row["year"] or 2017), int(row["month"]), int(row["day"])) + timedelta(
            hours=int(row["hour"] or 0), minutes=int(row["minute"] or 0)
        )
        samples.setdefault(stamp, {})[(row["variable"], str(row["key"]).upper())] = float(
            row["value"]
        )

    observations: list[BuildingState] = []
    for stamp in sorted(samples):
        channels = samples[stamp]

        def get(variable: str, key: str, _channels=channels) -> float | None:
            return _channels.get((variable, key.upper()))

        zones: list[ZoneState] = []
        for binding in model.zones:
            air_temp = get(VAR_ZONE_AIR_TEMP, binding.zone)
            if air_temp is None:
                continue
            occupancy = get(VAR_OCCUPANCY, binding.zone)
            zones.append(
                ZoneState(
                    zone=binding.zone,
                    air_temp_c=air_temp,
                    occupancy=max(0.0, occupancy) if occupancy is not None else None,
                    cooling_setpoint_c=get(VAR_COOLING_SETPOINT, binding.zone),
                    heating_setpoint_c=get(VAR_HEATING_SETPOINT, binding.zone),
                    pmv=get(VAR_PMV, binding.people) if binding.people else None,
                )
            )
        if not zones:
            continue

        outdoor = get(VAR_OUTDOOR_TEMP, "Environment")
        observations.append(
            BuildingState(
                sim_time=stamp,
                outdoor_air_temp_c=outdoor if outdoor is not None else 0.0,
                facility_power_w=0.0,
                zones=zones,
            )
        )

    return observations


__all__ = [
    "ChunkReader",
    "ChunkRunner",
    "HorizonChunk",
    "RecedingHorizonDriver",
    "RecedingStats",
    "breakpoints_for_chunk",
    "compact_fields",
    "plan_chunks",
    "read_chunk_results",
]
