"""Populate a decision journal with real planning cycles, for the dashboard/video.

The live A/B agent arm can't land a plan on CPU-only hardware (one constrained-decode call
takes tens of seconds while EnergyPlus finishes the week in ~2s), so its ``plans`` table - and
therefore the dashboard's decision journal - comes out empty. This script fills it *honestly*:
it takes real observed states from a completed run's telemetry, drives each through the **real**
``Planner`` and ``Guardian``, and journals every cycle (proposed plan, LLM call, guardian
verdict) exactly the way the live executor would - just not racing the simulation clock.

It writes into a **copy** of the source telemetry database, so the result is one self-consistent
file the dashboard can point at: real telemetry (comfort strip, race chart) *and* a populated
journal. The original A/B database is left untouched.

Requires a running Ollama with the model pulled (it makes one real LLM call per cycle). No
EnergyPlus needed - the states are replayed from telemetry that a real EnergyPlus run produced.
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from agent.digest import build_digest, load_forecast
from agent.planner import Planner
from common.config import Settings
from common.log import get_logger
from common.models import (
    BuildingState,
    GuardianDecision,
    GuardianEvent,
    GuardianStatus,
    PreparedModel,
    TriggerEnum,
    ZoneState,
)
from common.store import TelemetryStore, read_telemetry
from experiments.kpis import load_carbon, load_tariff
from experiments.smoke_llm_loop import _baseline_map
from guardian.core import Guardian, RateHistory
from simulation.run_baseline import _read_epw_drybulb

log = get_logger("experiments.journal_demo")

RESULTS_ROOT = "experiments/results"


def _states_from_telemetry(frame: pd.DataFrame) -> list[BuildingState]:
    """Reconstruct per-timestep BuildingStates from a telemetry frame (grouped by sim_time)."""
    states: list[BuildingState] = []
    for sim_time, rows in frame.groupby("sim_time"):
        first = rows.iloc[0]
        zones = [
            ZoneState(
                zone=r["zone"], air_temp_c=float(r["air_temp_c"]),
                occupancy=None if pd.isna(r["occupancy"]) else float(r["occupancy"]),
                pmv=None if pd.isna(r["pmv"]) else float(r["pmv"]),
                cooling_setpoint_c=None if pd.isna(r["cooling_setpoint_c"])
                else float(r["cooling_setpoint_c"]),
                heating_setpoint_c=None if pd.isna(r["heating_setpoint_c"])
                else float(r["heating_setpoint_c"]),
            )
            for _, r in rows.iterrows()
        ]
        states.append(BuildingState(
            sim_time=sim_time.to_pydatetime() if hasattr(sim_time, "to_pydatetime") else sim_time,
            outdoor_air_temp_c=float(first["outdoor_air_temp_c"] or 0.0),
            facility_power_w=float(first["facility_power_w"] or 0.0),
            zones=zones,
        ))
    return states


def _pick_cycles(states: list[BuildingState], n: int) -> list[BuildingState]:
    """Pick ``n`` occupied states spread across the run (one per distinct hour, busiest first)."""
    occupied = [s for s in states if sum(z.occupancy or 0 for z in s.zones) > 0]
    by_hour: dict[int, BuildingState] = {}
    for s in sorted(occupied, key=lambda s: -sum(z.occupancy or 0 for z in s.zones)):
        by_hour.setdefault(s.sim_time.hour, s)
    chosen = sorted(by_hour.values(), key=lambda s: s.sim_time)
    return chosen[:n] if len(chosen) >= n else chosen


def run_journal_demo(
    settings: Settings, *, source_db: Path, out_db: Path, cycles: int, timeout_s: float
) -> Path:
    model = PreparedModel.load(settings.simulation_dir / "agentic_model.json")
    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")
    epw = _read_epw_drybulb(settings.epw_path)
    baseline_map = _baseline_map(model)

    frame = read_telemetry(source_db)
    if frame.empty:
        raise SystemExit(f"{source_db} has no telemetry to replay")
    states = _pick_cycles(_states_from_telemetry(frame), cycles)
    if not states:
        raise SystemExit("no occupied states found in the source telemetry")
    log.info("replaying %d planning cycle(s) from %s", len(states), source_db)

    out_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_db, out_db)  # keep the real telemetry; add a journal on top

    run_id = "journal-demo"
    guardian = Guardian()
    history = RateHistory.empty()
    made = clipped = 0

    with TelemetryStore(out_db) as store:
        store.start_run(run_id, label="journal_demo", notes="replayed real states")
        planner = Planner(
            model=settings.ollama_model, host=settings.ollama_host, timeout_s=timeout_s,
            keep_alive=settings.ollama_keep_alive, store=store, run_id=run_id,
        )
        for i, state in enumerate(states, 1):
            trigger = TriggerEnum.STARTUP if i == 1 else TriggerEnum.HOURLY
            forecast = load_forecast(
                state.sim_time, epw_drybulb=epw, tariff=tariff, carbon=carbon, hours=6
            )
            digest = build_digest(state, forecast=forecast)
            print(f"  cycle {i}/{len(states)}  {state.sim_time:%m-%d %H:%M}  -> calling model ...")
            plan = planner.plan(digest, now=state.sim_time, trigger=trigger)
            if plan is None:
                print(f"  cycle {i}: no plan (model slow/unreachable) - skipped")
                continue
            made += 1
            # Journal the proposed plan, then run it through the real guardian and journal the
            # verdict - exactly the pair the live executor records.
            store.write_plan(plan, run_id=run_id)
            setpoints = plan.to_setpoint_plan(now=state.sim_time, baseline=baseline_map)
            verdicts = [guardian.filter(setpoints, z, history) for z in state.zones]
            reasons = [r for v in verdicts for r in v.reasons]
            if any(v.status is GuardianStatus.REJECTED for v in verdicts):
                decision = GuardianDecision.REJECTED
            elif reasons:
                decision = GuardianDecision.CLAMPED
                clipped += 1
            else:
                decision = GuardianDecision.ACCEPTED
            store.write_guardian_event(
                GuardianEvent(
                    at=state.sim_time, plan_id=plan.plan_id, decision=decision,
                    note="; ".join(reasons[:12]) or "accepted, no changes",
                ),
                run_id=run_id,
            )
            ecms = ", ".join(e.value for e in plan.ecms) or "(hold)"
            print(f"  cycle {i}: {decision}  ecms={ecms}  ({len(plan.actions)} action(s))")

    log.info("journal demo: %d plan(s) journalled, %d clipped -> %s", made, clipped, out_db)
    return out_db


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Populate a real decision journal for the demo.")
    parser.add_argument("--source-db", type=Path, required=True,
                         help="telemetry .sqlite from a completed run (e.g. an A/B agent arm)")
    parser.add_argument("--cycles", type=int, default=6, help="how many planning cycles to journal")
    parser.add_argument("--timeout", type=float, default=200.0, help="LLM timeout seconds")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    out_db = args.out or (
        settings.repo_root / RESULTS_ROOT / f"journal_demo_{datetime.now():%Y%m%dT%H%M%S}"
        / "hive.sqlite"
    )
    run_journal_demo(
        settings, source_db=args.source_db, out_db=out_db, cycles=args.cycles,
        timeout_s=args.timeout,
    )
    print(f"\nJournal-populated database ready:\n  {out_db}")
    print("In the dashboard sidebar, select this database under 'Telemetry DB'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
