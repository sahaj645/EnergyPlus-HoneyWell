"""Proof that the sensor -> actuator round trip works, with no LLM anywhere.

The competition's H6 tripwire is "the loop does not actually close". This harness is the
evidence against it, and it is deliberately dumb: a hardcoded plan that raises the cooling
setpoint by 2 C between 14:00 and 16:00, applied through the real guardian and the real bus,
against the real EnergyPlus runtime.

It runs **two** one-day simulations - control (no plan) and agent (dumb plan) - because a
single run cannot distinguish "our setback worked" from "it got hotter outside in the
afternoon". Identical IDF, identical weather, identical run period; the only difference is
whether a plan is supplied. Two independent things then have to be true:

1. **The write landed.** ``Zone Thermostat Cooling Setpoint Temperature`` - which EnergyPlus
   reports back to us, not a value we echo - moves during the window in the agent run and not
   in the control run. This proves actuation reached the simulation's internal state.
2. **The building responded.** Mean zone air temperature during the window is *higher* in the
   agent run. This proves the actuation had physical consequences.

(1) without (2) means we wrote to something inert. (2) without (1) means we are reading noise.
Both together is a closed loop.

Run it::

    python -m experiments.smoke_roundtrip
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from agent.bus import SimulationBus
from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import Actuator, ApprovedPlan, BuildingState, Plan, PlanStep, PreparedModel
from common.store import TelemetryStore, read_telemetry
from guardian.supervisor import Guardian
from simulation.receding import RecedingHorizonDriver
from simulation.snapshots import SnapshotWriter, summarize

log = get_logger("experiments.smoke")

MODE_LIVE = "live"
MODE_RECEDING = "receding"

#: Receding mode sets the RunPeriod itself, so it needs a date; live mode takes it from the
#: IDF. Mid-July is a hot day in a cooling-dominated Indian climate, which is where a setback
#: has something to act on.
DEFAULT_START = date(2017, 7, 15)

SETBACK_START_HOUR = 14
SETBACK_END_HOUR = 16
SETBACK_DELTA_C = 2.0

#: Minimum temperature rise we require inside the window to call the loop closed. Small on
#: purpose: a one-day run with a modest setback moves a well-conditioned zone by a few tenths,
#: and inflating this into a "big number" test would make the proof fragile, not stronger.
MIN_TEMP_RESPONSE_C = 0.05

#: Minimum setpoint movement to consider the actuator write observed.
MIN_SETPOINT_RESPONSE_C = 0.5


# --------------------------------------------------------------------------------------
# Assertion logic (pure - unit-tested without EnergyPlus)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTripResult:
    """Outcome of comparing a control run against an agent run."""

    setpoint_delta_c: float
    temp_delta_c: float
    control_temp_c: float
    agent_temp_c: float
    samples: int
    setpoint_moved: bool
    building_responded: bool

    @property
    def ok(self) -> bool:
        return self.setpoint_moved and self.building_responded and self.samples > 0

    def describe(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        return (
            f"[{verdict}] window samples={self.samples}\n"
            f"  setpoint delta (agent - control): {self.setpoint_delta_c:+.3f} C "
            f"({'observed' if self.setpoint_moved else 'NOT observed'})\n"
            f"  zone temp      control={self.control_temp_c:.3f} C  "
            f"agent={self.agent_temp_c:.3f} C  delta={self.temp_delta_c:+.3f} C "
            f"({'responded' if self.building_responded else 'NO response'})"
        )


def _window(frame: pd.DataFrame, start_hour: int, end_hour: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    hours = pd.to_datetime(frame["sim_time"]).dt.hour
    return frame[(hours >= start_hour) & (hours < end_hour)]


def evaluate_roundtrip(
    control: pd.DataFrame,
    agent: pd.DataFrame,
    *,
    start_hour: int = SETBACK_START_HOUR,
    end_hour: int = SETBACK_END_HOUR,
    min_temp_response_c: float = MIN_TEMP_RESPONSE_C,
    min_setpoint_response_c: float = MIN_SETPOINT_RESPONSE_C,
) -> RoundTripResult:
    """Compare two telemetry frames over the setback window.

    Pure: takes frames, returns a verdict. No database, no EnergyPlus, no clock.
    """
    control_window = _window(control, start_hour, end_hour)
    agent_window = _window(agent, start_hour, end_hour)
    samples = min(len(control_window), len(agent_window))

    if samples == 0:
        return RoundTripResult(0.0, 0.0, 0.0, 0.0, 0, False, False)

    control_temp = float(control_window["air_temp_c"].mean())
    agent_temp = float(agent_window["air_temp_c"].mean())
    temp_delta = agent_temp - control_temp

    control_sp = float(control_window["cooling_setpoint_c"].mean())
    agent_sp = float(agent_window["cooling_setpoint_c"].mean())
    setpoint_delta = agent_sp - control_sp

    return RoundTripResult(
        setpoint_delta_c=setpoint_delta,
        temp_delta_c=temp_delta,
        control_temp_c=control_temp,
        agent_temp_c=agent_temp,
        samples=samples,
        setpoint_moved=setpoint_delta >= min_setpoint_response_c,
        building_responded=temp_delta >= min_temp_response_c,
    )


# --------------------------------------------------------------------------------------
# The dumb plan
# --------------------------------------------------------------------------------------


def build_dumb_plan(
    model: PreparedModel,
    baseline_cooling_c: float,
    *,
    start_hour: int = SETBACK_START_HOUR,
    end_hour: int = SETBACK_END_HOUR,
    delta_c: float = SETBACK_DELTA_C,
) -> Plan:
    """A hardcoded setback: +``delta_c`` on cooling between the two hours, then back.

    Offsets are minutes from the plan anchor. The bus anchors a plan to the simulation time at
    which it first sees it - the first post-warmup timestep, i.e. shortly after midnight - so
    hour-of-day maps onto ``hour * 60``.
    """
    steps: list[PlanStep] = []
    for binding in model.zones:
        if not binding.cooling_schedule:
            continue
        steps.append(
            PlanStep(
                offset_minutes=start_hour * 60,
                zone=binding.zone,
                actuator=Actuator.COOLING_SETPOINT_C,
                value=baseline_cooling_c + delta_c,
            )
        )
        steps.append(
            PlanStep(
                offset_minutes=end_hour * 60,
                zone=binding.zone,
                actuator=Actuator.COOLING_SETPOINT_C,
                value=baseline_cooling_c,
            )
        )
    return Plan(
        planner_model="hardcoded-dumb-plan",
        horizon_minutes=24 * 60,
        steps=steps,
        rationale=f"smoke test: +{delta_c} C cooling setback {start_hour}:00-{end_hour}:00",
    )


def _seed_state(model: PreparedModel, baseline_cooling_c: float) -> BuildingState:
    """A synthetic observation for the guardian's first review.

    The guardian rate-limits against the *observed* setpoint, and at plan-construction time we
    have not run yet - so we hand it the baseline the IDF was prepared with, which is exactly
    what the simulation will start from.
    """
    from common.models import ZoneState

    return BuildingState(
        sim_time=datetime(2017, 1, 1),
        outdoor_air_temp_c=30.0,
        facility_power_w=0.0,
        zones=[
            ZoneState(
                zone=binding.zone,
                air_temp_c=baseline_cooling_c,
                cooling_setpoint_c=baseline_cooling_c,
                heating_setpoint_c=baseline_cooling_c - 3.0,
            )
            for binding in model.zones
        ],
    )


def _baseline_cooling(model: PreparedModel) -> float:
    """The cooling setpoint ``prepare_idf`` initialised the schedules to."""
    values = [
        model.constant_schedules[binding.cooling_schedule]
        for binding in model.zones
        if binding.cooling_schedule and binding.cooling_schedule in model.constant_schedules
    ]
    if not values:
        raise ValueError("no cooling Schedule:Constant found in the prepared model")
    return sum(values) / len(values)


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------


def _run_arm(
    label: str,
    *,
    model: PreparedModel,
    settings: Settings,
    approved: ApprovedPlan | None,
    out_root: Path,
    db_path: Path,
    mode: str = MODE_LIVE,
    snapshot_writer: SnapshotWriter | None = None,
    start_date: date | None = None,
    days: int = 1,
) -> str:
    """Run one arm for a single day in either actuation mode. Returns its ``run_id``."""
    run_id = f"smoke-{label}"
    out_dir = out_root / f"out_smoke_{label}"

    provider = None
    if approved is not None:
        def provider(_now: datetime, _plan: ApprovedPlan = approved) -> ApprovedPlan:
            return _plan

    with TelemetryStore(db_path, flush_every_timesteps=12) as store:
        if approved is not None:
            store.write_approved_plan(approved, run_id=run_id)

        if mode == MODE_RECEDING:
            driver = RecedingHorizonDriver(
                model=model,
                store=store,
                run_id=run_id,
                epw_path=settings.epw_path,
                out_dir=out_dir,
                start_date=start_date or DEFAULT_START,
                total_days=days,
                plan_provider=provider,
                snapshot_writer=snapshot_writer,
            )
            exit_code = driver.run()
            summary = driver.stats.summary()
            recorded = driver.stats.timesteps_recorded
        else:
            bus = SimulationBus(
                model=model,
                store=store,
                run_id=run_id,
                epw_path=settings.epw_path,
                out_dir=out_dir,
                plan_provider=provider,
            )
            exit_code = bus.run()
            summary = bus.stats.summary()
            recorded = bus.stats.timesteps
            # Materialise the version series *after* the run: writing an IDF inside the
            # callback would be blocking I/O on the hot path (rule R1).
            if snapshot_writer is not None:
                for state in bus.control_history:
                    snapshot_writer.commit(state)

    log.info("arm %s (%s): exit=%s %s", label, mode, exit_code, summary)
    if exit_code != 0:
        raise RuntimeError(f"arm {label} failed with exit code {exit_code}; see {out_dir}")
    if recorded == 0:
        raise RuntimeError(f"arm {label} recorded no timesteps; see {out_dir}")
    return run_id


def run_smoke(
    settings: Settings | None = None,
    *,
    db_path: Path | None = None,
    mode: str = MODE_LIVE,
    start_date: date | None = None,
    days: int = 1,
) -> RoundTripResult:
    """Run both arms and evaluate. Requires EnergyPlus and a prepared IDF."""
    settings = settings or Settings.from_env()
    eplus_path.require_energyplus()

    index_path = settings.simulation_dir / "agentic_model.json"
    if not index_path.is_file():
        raise FileNotFoundError(
            f"{index_path} not found. Run `python -m simulation.prepare_idf` first."
        )
    model = PreparedModel.load(index_path)

    db_path = db_path or (settings.repo_root / "hive_smoke.sqlite")
    if db_path.exists():
        db_path.unlink()

    baseline_cooling = _baseline_cooling(model)
    plan = build_dumb_plan(model, baseline_cooling)

    # Rule R2: even a hardcoded plan goes through the guardian. There is no other way to get
    # an ApprovedPlan, which is the only thing the bus will actuate.
    approved = Guardian().review(plan, _seed_state(model, baseline_cooling))
    log.info(
        "guardian verdict=%s steps=%d violations=%d",
        approved.decision,
        len(approved.steps),
        len(approved.violations),
    )
    if not approved.steps:
        raise RuntimeError(
            f"guardian left no actuatable steps (decision={approved.decision}); "
            "the smoke plan is outside the safety envelope"
        )

    # Only the agent arm produces a version series - the control arm is the unmodified model
    # by definition, so snapshotting it would add nothing to deliverable #2.
    snapshot_writer = SnapshotWriter(
        base_idf=model.idf_path,
        versions_dir=settings.simulation_dir / "versions",
    )

    # Both arms use the same IDF - the only difference is whether a plan reaches the
    # controller. In receding mode the driver sets the RunPeriod per chunk; in live mode it
    # comes from the IDF.
    common = {
        "model": model,
        "settings": settings,
        "out_root": settings.simulation_dir,
        "db_path": db_path,
        "mode": mode,
        "start_date": start_date,
        "days": days,
    }
    control_id = _run_arm("control", approved=None, **common)
    agent_id = _run_arm("agent", approved=approved, snapshot_writer=snapshot_writer, **common)

    log.info("version series: %d file(s)", snapshot_writer.version_count)
    print(summarize(settings.simulation_dir / "versions"))

    control = read_telemetry(db_path, run_id=control_id)
    agent = read_telemetry(db_path, run_id=agent_id)
    return evaluate_roundtrip(control, agent)


def parse_start(value: str) -> date:
    """Parse ``MM-DD`` into a date on the nominal non-leap calendar the RunPeriods use."""
    month, day = (int(part) for part in value.split("-", 1))
    return date(2017, month, day)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove the closed loop without an LLM.")
    parser.add_argument("--db", default=None, help="telemetry database path")
    parser.add_argument(
        "--mode",
        choices=[MODE_LIVE, MODE_RECEDING],
        default=MODE_LIVE,
        help="live = runtime-API actuation; receding = re-simulate each horizon chunk",
    )
    parser.add_argument(
        "--start",
        type=parse_start,
        default=DEFAULT_START,
        help="MM-DD start date (receding mode only; live mode uses the IDF's RunPeriod)",
    )
    parser.add_argument("--days", type=int, default=1, help="days to simulate (receding mode)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    result = run_smoke(
        settings,
        db_path=Path(args.db) if args.db else None,
        mode=args.mode,
        start_date=args.start,
        days=args.days,
    )
    print(result.describe())
    return 0 if result.ok else 1


__all__ = [
    "MIN_SETPOINT_RESPONSE_C",
    "MIN_TEMP_RESPONSE_C",
    "SETBACK_DELTA_C",
    "SETBACK_END_HOUR",
    "SETBACK_START_HOUR",
    "RoundTripResult",
    "build_dumb_plan",
    "evaluate_roundtrip",
    "run_smoke",
]


if __name__ == "__main__":
    raise SystemExit(main())
