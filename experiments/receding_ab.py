"""Agent-arm economics via the receding-horizon driver.

The live A/B agent arm races real EnergyPlus wall-clock time: a whole week simulates in a few
seconds while one constrained-decode ``Plan`` call takes 30-100s+ on CPU-only hardware, so no
plan ever lands before the sim moves on (see README.md's honest note on this). This module runs
the exact same ``Planner``/``Guardian``/digest through
:class:`simulation.receding.RecedingHorizonDriver` instead: one plan per day-chunk, decided
*before* that chunk's EnergyPlus run starts, so a slow CPU-bound call costs wall-clock time,
not a discarded plan.

Day 1 has no prior observation to plan from (receding mode's `read_state()` returns the last
chunk's result) and runs on baseline; from day 2 onward each chunk's plan is a real Ollama call
against the previous day's actual telemetry.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import PreparedModel, TriggerEnum
from common.store import TelemetryStore
from experiments.ab import _baseline_map, prepare_arm_idf
from experiments.kpis import compute_kpis, load_carbon, load_tariff
from simulation.run_baseline import RunPeriodSpec, hottest_week

log = get_logger("experiments.receding_ab")

RESULTS_ROOT = "experiments/results"
_YEAR = 2001  # arbitrary non-leap year; RunPeriod carries no year, only month/day


def _span_days(spec: RunPeriodSpec) -> tuple[date, int]:
    start = date(_YEAR, spec.begin_month, spec.begin_day)
    end = date(_YEAR, spec.end_month, spec.end_day)
    return start, (end - start).days + 1


def run_receding_agent_arm(
    settings: Settings, spec: RunPeriodSpec, *, out_dir: Path, install_dir, timeout_s: float
) -> Path:
    from agent.digest import build_digest, load_forecast
    from agent.planner import Planner
    from guardian.core import Guardian, RateHistory
    from simulation.receding import RecedingHorizonDriver
    from simulation.run_baseline import _read_epw_drybulb

    model = PreparedModel.load(settings.simulation_dir / "agentic_model.json")
    out_dir.mkdir(parents=True, exist_ok=True)
    patched = out_dir / "agent_patched.idf"
    prepare_arm_idf(
        Path(model.idf_path), spec, timesteps_per_hour=6, out_path=patched, install_dir=install_dir
    )

    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")
    epw_series = _read_epw_drybulb(settings.epw_path)
    baseline_map = _baseline_map(model)

    planner = Planner(
        model=settings.ollama_model, host=settings.ollama_host, timeout_s=timeout_s,
        keep_alive=settings.ollama_keep_alive,
    )
    guardian = Guardian()
    rate_history = RateHistory.empty()

    def forecast(now: datetime):
        return load_forecast(now, epw_drybulb=epw_series, tariff=tariff, carbon=carbon, hours=6)

    db_path = out_dir / "hive.sqlite"
    if db_path.exists():
        db_path.unlink()

    start_date, total_days = _span_days(spec)

    driver = RecedingHorizonDriver(
        model=model, store=TelemetryStore(db_path), run_id="receding-agent",
        epw_path=settings.epw_path, out_dir=out_dir, start_date=start_date,
        total_days=total_days, horizon_days=1, idf_path=patched, install_dir=install_dir,
    )

    def plan_provider(now: datetime):
        nonlocal rate_history
        state = driver.read_state()
        if state is None:
            log.info("chunk at %s: no prior observation yet - baseline", now)
            return None
        digest = build_digest(state, forecast=forecast(now))
        plan = planner.plan(digest, now=now, trigger=TriggerEnum.HOURLY)
        if plan is None:
            log.warning("chunk at %s: planner returned no plan - baseline", now)
            return None
        setpoints = plan.to_setpoint_plan(now=now, baseline=baseline_map)
        verdicts = [guardian.filter(setpoints, zone, rate_history) for zone in state.zones]
        for verdict in verdicts:
            for step in verdict.safe_plan.steps:
                rate_history = rate_history.record(step.zone, now, step.value)
        approved = guardian.approve(verdicts, plan_id=plan.plan_id, now=now)
        log.info("chunk at %s: plan %s -> %s (%d step(s))", now, plan.plan_id, approved.decision,
                 len(approved.steps))
        return approved

    driver.plan_provider = plan_provider
    exit_code = driver.run()
    driver.store.close()
    log.info("receding agent arm: exit=%s %s", exit_code, driver.stats.summary())
    return out_dir


def _aggregate_kpis(out_dir: Path, tariff_path: Path, carbon_path: Path) -> dict:
    energy = cost = carbon = 0.0
    peak = 0.0
    for chunk_dir in sorted(out_dir.glob("chunk_*")):
        sql = chunk_dir / "eplusout.sql"
        if not sql.is_file():
            continue
        k = compute_kpis(sql, tariff_path, carbon_path, run_label=chunk_dir.name)
        energy += k.site_kwh
        cost += k.cost_inr
        carbon += k.carbon_kg
        peak = max(peak, k.peak_demand_kw)
    return {"site_kwh": energy, "cost_inr": cost, "carbon_kg": carbon, "peak_demand_kw": peak}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent-arm economics via receding-horizon mode.")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--baseline-sql", type=Path, required=True,
                         help="eplusout.sql from a real baseline arm run, same RunPeriod")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    install_dir = eplus_path.require_energyplus()
    spec = hottest_week(settings.epw_path)
    out_dir = settings.repo_root / RESULTS_ROOT / f"receding_agent_{datetime.now():%Y%m%dT%H%M%S}"

    run_receding_agent_arm(settings, spec, out_dir=out_dir, install_dir=install_dir,
                            timeout_s=args.timeout)

    tariff_path = settings.data_dir / "tariff.csv"
    carbon_path = settings.data_dir / "carbon_intensity.csv"
    agent_kpis = _aggregate_kpis(out_dir, tariff_path, carbon_path)
    baseline_kpis = compute_kpis(args.baseline_sql, tariff_path, carbon_path, run_label="baseline")

    print(f"\nbaseline : {baseline_kpis.site_kwh:.1f} kWh, INR {baseline_kpis.cost_inr:.1f}, "
          f"{baseline_kpis.carbon_kg:.2f} kg, peak {baseline_kpis.peak_demand_kw:.2f} kW")
    print(f"agent    : {agent_kpis['site_kwh']:.1f} kWh, INR {agent_kpis['cost_inr']:.1f}, "
          f"{agent_kpis['carbon_kg']:.2f} kg, peak {agent_kpis['peak_demand_kw']:.2f} kW")
    if baseline_kpis.site_kwh:
        pct = 100 * (baseline_kpis.site_kwh - agent_kpis["site_kwh"]) / baseline_kpis.site_kwh
        print(f"site kWh delta (baseline -> agent): {pct:+.1f}%")
    print(f"out_dir: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
