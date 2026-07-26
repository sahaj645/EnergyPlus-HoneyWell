"""A/B harness: baseline vs the full agent loop, under identical conditions.

This is the run that produces the scored number. Everything else in the repo exists to make
this comparison honest: same IDF ancestry, same EPW, same ``RunPeriod`` (the chosen summer
week), same timestep, for every arm. The only thing that differs between arms is whether - and
how - something is driving the setpoints.

Three arms, the first two with **no agent at all**:

* **``baseline``** (always run) - ``baseline.idf`` unmodified: the prototype's original
  ``Schedule:Compact`` day/night setback profile, verbatim. This is the true control arm.
* **``constant``** (``--secondary-baseline constant``, optional, clearly labeled) - a copy of
  ``agentic.idf`` with nothing actuating it. Its ``Schedule:Constant`` objects hold whatever
  value ``prepare_idf`` set them to and EnergyPlus simply runs the static IDF - no Python
  callback touches an actuator. This isolates "the flattened schedule alone" from "the flattened
  schedule *plus* an agent", which is the fair way to ask how much of any saving is the agent's
  doing versus an artifact of losing the setback profile.
* **``agent``** (always run) - the full closed loop: ``SimulationBus`` + ``guardian.Executor``
  + ``Scheduler`` + ``Planner`` + ``PlanCache`` + ``DriftEventDetector``, exactly what
  ``experiments.smoke_llm_loop`` exercises for one day, generalised to the chosen week.

All three IDFs are prepared by the same function (:func:`prepare_arm_idf`): set the shared
``RunPeriod``/timestep/``Output:SQLite``/meters, ensure Fanger PMV + the zone/site output
variables the report needs, save as a **new** file. Neither ``baseline.idf`` nor ``agentic.idf``
is ever edited in place.

Requires EnergyPlus and (for the agent arm) a running Ollama with the model pulled. Nothing
here is exercised in CI - this module is import-safe, but every entry point that touches
EnergyPlus fails fast with a clear message if the install is missing.

This supersedes the ``experiments/ab_harness.py`` scaffold.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import Actuator, BuildingState, PreparedModel
from common.planslot import PlanSlot
from common.store import TelemetryStore
from experiments.kpis import load_carbon, load_tariff
from simulation.run_baseline import RunPeriodSpec, count_errors, hottest_week, month_spec, patch_idf

log = get_logger("experiments.ab")

RESULTS_ROOT = "experiments/results"

BASELINE_LABEL = "baseline"
CONSTANT_LABEL = "constant"
AGENT_LABEL = "agent"


# --------------------------------------------------------------------------------------
# Shared IDF preparation - identical treatment for every arm
# --------------------------------------------------------------------------------------


def prepare_arm_idf(
    source_idf: Path,
    spec: RunPeriodSpec,
    *,
    timesteps_per_hour: int,
    out_path: Path,
    install_dir: Path,
) -> dict[str, str]:
    """Patch a copy of ``source_idf`` for one arm: shared RunPeriod, comfort outputs.

    Never mutates ``source_idf``. Applies exactly the same three steps regardless of which arm
    calls it, which is what makes "identical conditions" checkable by reading this function
    rather than trusting that three call sites agree:

    1. :func:`simulation.run_baseline.patch_idf` - the shared ``RunPeriod``, sub-hourly
       timestep, ``Output:SQLite``, and the meters the KPI/report extraction needs.
    2. :func:`simulation.prepare_idf.ensure_fanger_comfort` - so PMV is computable for the
       comfort table, even on ``baseline.idf`` (which prepare_idf never touches).
    3. :func:`simulation.prepare_idf.ensure_outputs` - the zone/site Output:Variable requests,
       keyed correctly (PMV by People name, everything else by zone name).

    Returns the zone -> People-object-name map, needed to read PMV back out of the SQL later.
    """
    from simulation.idf_io import load_idf, save_idf
    from simulation.prepare_idf import _thermostat_schedules, ensure_fanger_comfort, ensure_outputs

    idf = load_idf(source_idf, install_dir)
    patch_idf(idf, spec, timesteps_per_hour=timesteps_per_hour)

    zone_map = _thermostat_schedules(idf)
    zones = sorted(zone_map)
    zone_people = ensure_fanger_comfort(idf)
    ensure_outputs(idf, zones, sorted(set(zone_people.values())))

    save_idf(idf, out_path)
    return zone_people


# --------------------------------------------------------------------------------------
# Bare runs (no agent): baseline and the naive-constant secondary baseline
# --------------------------------------------------------------------------------------


def run_bare(idf_path: Path, epw_path: Path, out_dir: Path) -> int:
    """Run EnergyPlus with no Python callback at all. Returns the exit code.

    Used for arms where nothing is supposed to be actuating: the setpoints in force are exactly
    whatever the (patched, but schedule-untouched) IDF says, for the whole run.
    """
    eplus_path.require_energyplus()
    from pyenergyplus.api import EnergyPlusAPI

    out_dir.mkdir(parents=True, exist_ok=True)
    api = EnergyPlusAPI()
    state = api.state_manager.new_state()
    try:
        return api.runtime.run_energyplus(
            state, ["-w", str(epw_path), "-d", str(out_dir), str(idf_path)]
        )
    finally:
        api.state_manager.delete_state(state)


@dataclass(frozen=True)
class BareArmResult:
    label: str
    out_dir: Path
    idf_path: Path
    exit_code: int
    severe: int
    fatal: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.fatal == 0


def run_bare_arm(
    label: str,
    source_idf: Path,
    spec: RunPeriodSpec,
    *,
    epw_path: Path,
    timesteps_per_hour: int,
    out_dir: Path,
    install_dir: Path,
) -> BareArmResult:
    """Prepare + run one no-agent arm (``baseline`` or ``constant``)."""
    patched = out_dir / f"{label}_patched.idf"
    prepare_arm_idf(
        source_idf, spec, timesteps_per_hour=timesteps_per_hour, out_path=patched,
        install_dir=install_dir,
    )
    exit_code = run_bare(patched, epw_path, out_dir)
    severe, fatal = count_errors(out_dir / "eplusout.err")
    result = BareArmResult(
        label=label, out_dir=out_dir, idf_path=patched, exit_code=exit_code,
        severe=severe, fatal=fatal,
    )
    log.info(
        "arm %s: exit=%s severe=%d fatal=%d -> %s", label, exit_code, severe, fatal, out_dir
    )
    if not result.ok:
        raise RuntimeError(
            f"arm {label!r} did not complete cleanly (exit={exit_code}, fatal={fatal}); "
            f"see {out_dir / 'eplusout.err'}"
        )
    if severe:
        log.warning(
            "arm %s: %d Severe error(s) - inspect before trusting its numbers", label, severe
        )
    return result


# --------------------------------------------------------------------------------------
# The agent arm: the full closed loop, reusable per-week (here) and per-day (endurance.py)
# --------------------------------------------------------------------------------------


class _StateHolder:
    """Thread-safe capture of recent building states, written on the callback thread.

    Same shape as ``experiments.smoke_llm_loop.StateHolder`` - duplicated rather than imported
    so this module has no dependency on a one-day smoke script; both are small and stable.
    """

    def __init__(self, maxlen: int = 6) -> None:
        import threading

        self._lock = threading.Lock()
        self._states: deque[BuildingState] = deque(maxlen=maxlen)

    def push(self, state: BuildingState) -> None:
        with self._lock:
            self._states.append(state)

    def latest(self) -> BuildingState | None:
        with self._lock:
            return self._states[-1] if self._states else None

    def history(self) -> list[BuildingState]:
        with self._lock:
            return list(self._states)[:-1]


@dataclass
class AgentArmResult:
    label: str
    out_dir: Path
    idf_path: Path
    db_path: Path
    exit_code: int
    severe: int
    fatal: int
    timesteps: int
    plans_made: int
    accepted: int
    cache_hits: int
    holds: int
    events: int
    clipped_steps: int
    fallback_steps: int
    watchdog_trips: int
    timeouts: int
    scheduler_failed: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.fatal == 0

    def summary(self) -> str:
        return (
            f"timesteps={self.timesteps} plans_made={self.plans_made} accepted={self.accepted} "
            f"cache_hits={self.cache_hits} holds={self.holds} events={self.events} "
            f"clipped={self.clipped_steps} fallback={self.fallback_steps} "
            f"watchdog_trips={self.watchdog_trips} timeouts={self.timeouts} "
            f"scheduler_failed={self.scheduler_failed}"
        )


def run_agent_arm(
    *,
    model: PreparedModel,
    settings: Settings,
    spec: RunPeriodSpec,
    timesteps_per_hour: int,
    out_dir: Path,
    run_id: str,
    install_dir: Path,
    timeout_s: float = 30.0,
    cache=None,
    events=None,
) -> AgentArmResult:
    """Prepare a RunPeriod-scoped copy of ``agentic.idf`` and run the full live agent loop.

    This is the one building block both the week-long A/B ``agent`` arm and
    ``experiments.endurance``'s day-by-day chunks are made of. ``cache``/``events`` may be
    supplied by the caller so state (and, for the cache, hit-rate) carries across repeated
    calls within one process - the endurance runner does exactly this; a single-shot A/B run
    just lets fresh ones be created.
    """
    # Imported here, not at module level: keeps `experiments.ab` importable with no Ollama/
    # EnergyPlus present, exactly like every other harness in this repo.
    from agent.bus import SimulationBus
    from agent.cache import PlanCache
    from agent.digest import build_digest, load_forecast
    from agent.events import DriftEventDetector
    from agent.feedback import FeedbackTracker
    from agent.planner import Planner
    from agent.scheduler import Scheduler
    from guardian.core import Guardian as CoreGuardian
    from guardian.executor import Executor
    from simulation.run_baseline import _read_epw_drybulb

    out_dir.mkdir(parents=True, exist_ok=True)
    patched = out_dir / f"{AGENT_LABEL}_patched.idf"
    prepare_arm_idf(
        Path(model.idf_path), spec, timesteps_per_hour=timesteps_per_hour, out_path=patched,
        install_dir=install_dir,
    )

    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")
    cache = cache if cache is not None else PlanCache(tariff=tariff, carbon=carbon)
    events = events if events is not None else DriftEventDetector(
        tariff_band_at=cache.tariff_band_at,
        carbon_band_at=cache.carbon_band_at,
        err_path=out_dir / "eplusout.err",
    )

    epw_series = _read_epw_drybulb(settings.epw_path)

    def forecast(now: datetime):
        return load_forecast(now, epw_drybulb=epw_series, tariff=tariff, carbon=carbon, hours=6)

    baseline_map = _baseline_map(model)
    holder = _StateHolder()
    plan_slot = PlanSlot()
    db_path = out_dir / "hive.sqlite"
    if db_path.exists():
        db_path.unlink()

    flush_every = settings.telemetry_flush_every_timesteps
    with TelemetryStore(db_path, flush_every_timesteps=flush_every) as store:
        planner = Planner(
            model=settings.ollama_model,
            host=settings.ollama_host,
            timeout_s=timeout_s,
            keep_alive=settings.ollama_keep_alive,
            store=store,
            run_id=run_id,
        )

        executor = Executor(
            guardian=CoreGuardian(), model=model, plan_slot=plan_slot,
            plan_interval=timedelta(minutes=settings.plan_interval_minutes), run_id=run_id,
        )
        feedback = FeedbackTracker()

        def digest_provider(now: datetime) -> str:
            state = holder.latest()
            if state is None:
                return "SIM TIME: (no observation yet)"
            feedback.observe(executor.events_snapshot())
            return build_digest(
                state, history=holder.history(), forecast=forecast(now),
                active_plan=plan_slot.get(), feedback=feedback.pending_feedback(),
            )

        scheduler = Scheduler(
            planner=planner, plan_slot=plan_slot, digest_provider=digest_provider,
            baseline=baseline_map, timeout_s=timeout_s, store=store, run_id=run_id,
            event_detector=events, cache=cache, feedback=feedback,
        )
        bus = SimulationBus(
            model=model, store=store, run_id=run_id, epw_path=settings.epw_path,
            out_dir=out_dir, idf_path=patched,
        )
        executor.control = bus

        def provider(now: datetime):
            state = bus.read_state()
            if state is not None:
                holder.push(state)
                scheduler.on_timestep(now, state)
            return executor.provide(now)

        bus.plan_provider = provider
        exit_code = bus.run()
        scheduler.join(timeout=max(2.0, timeout_s))
        executor.drain_events(store, run_id=run_id)

    severe, fatal = count_errors(out_dir / "eplusout.err")
    result = AgentArmResult(
        label=AGENT_LABEL, out_dir=out_dir, idf_path=patched, db_path=db_path,
        exit_code=exit_code, severe=severe, fatal=fatal, timesteps=bus.stats.timesteps,
        plans_made=scheduler.stats.plans_made, accepted=scheduler.stats.accepted,
        cache_hits=scheduler.stats.cache_hits, holds=scheduler.stats.holds,
        events=scheduler.stats.events, clipped_steps=executor.stats.clipped_steps,
        fallback_steps=executor.stats.fallback_steps,
        watchdog_trips=executor.stats.watchdog_trips, timeouts=scheduler.stats.timeouts,
        scheduler_failed=scheduler.stats.failed,
    )
    log.info("arm agent: exit=%s severe=%d fatal=%d %s", exit_code, severe, fatal, result.summary())
    if not result.ok:
        raise RuntimeError(
            f"agent arm did not complete cleanly (exit={exit_code}, fatal={fatal}); "
            f"see {out_dir / 'eplusout.err'}"
        )
    if severe:
        log.warning("agent arm: %d Severe error(s) - inspect before trusting its numbers", severe)
    return result


def _baseline_map(model: PreparedModel) -> dict[tuple[str, str], float]:
    baseline: dict[tuple[str, str], float] = {}
    for binding in model.zones:
        if binding.cooling_schedule in model.constant_schedules:
            baseline[(binding.zone, Actuator.COOLING_SETPOINT_C.value)] = model.constant_schedules[
                binding.cooling_schedule
            ]
        if binding.heating_schedule in model.constant_schedules:
            baseline[(binding.zone, Actuator.HEATING_SETPOINT_C.value)] = model.constant_schedules[
                binding.heating_schedule
            ]
    return baseline


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


@dataclass
class AbResult:
    out_root: Path
    spec: RunPeriodSpec
    baseline: BareArmResult
    agent: AgentArmResult
    constant: BareArmResult | None = None

    @property
    def ok(self) -> bool:
        arms = [self.baseline, self.agent] + ([self.constant] if self.constant else [])
        return all(a.ok for a in arms)


def _resolve_spec(*, start: date | None, days: int | None, epw_path: Path) -> RunPeriodSpec:
    if start is not None:
        span = days or 7
        end = start + timedelta(days=span - 1)
        return RunPeriodSpec(
            label=f"ab_{start:%m%d}_{span}d", begin_month=start.month, begin_day=start.day,
            end_month=end.month, end_day=end.day,
        )
    if days is not None and days >= 28:
        # Loosely "a month" - the endurance runner drives this path one day at a time instead,
        # but a caller asking ab.py itself for a long window gets the calendar month closest to
        # the hottest week's start.
        anchor = hottest_week(epw_path)
        return month_spec(anchor.begin_month)
    return hottest_week(epw_path)


def run_ab(
    settings: Settings | None = None,
    *,
    start: date | None = None,
    days: int | None = None,
    timesteps_per_hour: int = 6,
    secondary_baseline: str | None = None,
    timeout_s: float = 30.0,
    out_root: Path | None = None,
) -> AbResult:
    """Run the full A/B comparison. Requires EnergyPlus (agent arm also needs Ollama)."""
    settings = settings or Settings.from_env()
    install_dir = eplus_path.require_energyplus()

    for path, hint in (
        (settings.idf_path, "fetch_assets"),
        (settings.epw_path, "fetch_assets"),
        (settings.simulation_dir / "agentic.idf", "prepare_idf"),
        (settings.simulation_dir / "agentic_model.json", "prepare_idf"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{path} not found. Run `python -m simulation.{hint}` first.")

    spec = _resolve_spec(start=start, days=days, epw_path=settings.epw_path)
    out_root = out_root or (
        settings.repo_root / RESULTS_ROOT / f"ab_{datetime.now():%Y%m%dT%H%M%S}"
    )
    out_root.mkdir(parents=True, exist_ok=True)
    log.info(
        "A/B run period: %s (%02d/%02d -> %02d/%02d) -> %s", spec.label, spec.begin_month,
        spec.begin_day, spec.end_month, spec.end_day, out_root,
    )

    model = PreparedModel.load(settings.simulation_dir / "agentic_model.json")

    baseline = run_bare_arm(
        BASELINE_LABEL, settings.idf_path, spec, epw_path=settings.epw_path,
        timesteps_per_hour=timesteps_per_hour, out_dir=out_root / BASELINE_LABEL,
        install_dir=install_dir,
    )

    constant: BareArmResult | None = None
    if secondary_baseline == "constant":
        constant = run_bare_arm(
            CONSTANT_LABEL, Path(model.idf_path), spec, epw_path=settings.epw_path,
            timesteps_per_hour=timesteps_per_hour, out_dir=out_root / CONSTANT_LABEL,
            install_dir=install_dir,
        )
    elif secondary_baseline is not None:
        raise ValueError(f"unknown --secondary-baseline {secondary_baseline!r}; only 'constant'")

    agent = run_agent_arm(
        model=model, settings=settings, spec=spec, timesteps_per_hour=timesteps_per_hour,
        out_dir=out_root / AGENT_LABEL, run_id=f"ab-{spec.label}", install_dir=install_dir,
        timeout_s=timeout_s,
    )

    result = AbResult(
        out_root=out_root, spec=spec, baseline=baseline, agent=agent, constant=constant
    )
    (out_root / "manifest.json").write_text(_manifest_json(result), encoding="utf-8")
    return result


def _bare_summary(arm: BareArmResult) -> dict:
    return {
        "out_dir": str(arm.out_dir),
        "exit_code": arm.exit_code,
        "severe": arm.severe,
        "fatal": arm.fatal,
    }


def _manifest_json(result: AbResult) -> str:
    import json

    agent_summary = _bare_summary(
        BareArmResult(
            label=result.agent.label, out_dir=result.agent.out_dir, idf_path=result.agent.idf_path,
            exit_code=result.agent.exit_code, severe=result.agent.severe, fatal=result.agent.fatal,
        )
    )
    agent_summary["counters"] = {
        k: v for k, v in vars(result.agent).items() if isinstance(v, int)
    }

    payload = {
        "out_root": str(result.out_root),
        "spec": {
            "label": result.spec.label,
            "begin_month": result.spec.begin_month,
            "begin_day": result.spec.begin_day,
            "end_month": result.spec.end_month,
            "end_day": result.spec.end_day,
        },
        "arms": {
            "baseline": _bare_summary(result.baseline),
            "agent": agent_summary,
        },
    }
    if result.constant is not None:
        payload["arms"]["constant"] = _bare_summary(result.constant)
    return json.dumps(payload, indent=2, default=str)


def parse_start(value: str) -> date:
    month, day = (int(part) for part in value.split("-", 1))
    return date(2017, month, day)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Baseline vs full agent loop, identical conditions."
    )
    parser.add_argument(
        "--start", type=parse_start, default=None, help="MM-DD; default: hottest week"
    )
    parser.add_argument("--days", type=int, default=None, help="span in days; default 7")
    parser.add_argument("--timestep", type=int, default=6, dest="timesteps_per_hour")
    parser.add_argument(
        "--secondary-baseline", choices=["constant"], default=None,
        help="also run a naive-constant-setpoint secondary baseline, clearly labeled",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="planner timeout, seconds")
    parser.add_argument("--out", type=Path, default=None, help="override the results directory")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    result = run_ab(
        settings, start=args.start, days=args.days, timesteps_per_hour=args.timesteps_per_hour,
        secondary_baseline=args.secondary_baseline, timeout_s=args.timeout, out_root=args.out,
    )

    print(f"A/B run complete -> {result.out_root}")
    print(f"  baseline : exit={result.baseline.exit_code} severe={result.baseline.severe}")
    if result.constant is not None:
        print(f"  constant : exit={result.constant.exit_code} severe={result.constant.severe}")
    print(f"  agent    : exit={result.agent.exit_code} severe={result.agent.severe}")
    print(f"  agent    : {result.agent.summary()}")
    print(f"next: python -m experiments.report --ab-dir {result.out_root}")
    return 0 if result.ok else 1


__all__ = [
    "AGENT_LABEL",
    "BASELINE_LABEL",
    "CONSTANT_LABEL",
    "AbResult",
    "AgentArmResult",
    "BareArmResult",
    "prepare_arm_idf",
    "run_ab",
    "run_agent_arm",
    "run_bare",
    "run_bare_arm",
]


if __name__ == "__main__":
    raise SystemExit(main())
