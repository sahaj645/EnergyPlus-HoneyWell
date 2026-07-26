"""Live one-day smoke of the full loop: EnergyPlus + guardian + real Ollama planner.

This is Session 5's exit gate. It runs one simulated day with the actual model in the actual
control loop and confirms, by eye, three things:

1. **The planner is really in the loop.** At least one plan is generated, accepted, and
   actuated - setpoints in telemetry move when a plan said they should.
2. **Nothing blocks the sim.** The LLM runs on a worker thread; the EnergyPlus callback only
   ever reads the latest committed plan. Run it with ``--timeout 0.1`` and every planning cycle
   is preempted, yet the day completes on baseline without stalling.
3. **The five-line scoreboard** (plans made / accepted / clipped / timeouts / cache) reflects
   what happened.

Requires EnergyPlus, a prepared IDF (``python -m simulation.prepare_idf``), and a running
Ollama with the model pulled. None of those exist in CI, so this module is import-safe but its
``run_llm_loop`` refuses to run without them.

Threading note: the EnergyPlus exchange may only be read from the callback thread, so the
callback captures each :class:`BuildingState` into a small locked holder; the planner worker
builds its digest from that snapshot, never by touching the live exchange.
"""

from __future__ import annotations

import argparse
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from agent.bus import SimulationBus
from agent.digest import ForecastRow, build_digest, load_forecast
from agent.planner import Planner
from agent.scheduler import Scheduler
from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import Actuator, BuildingState, PreparedModel
from common.planslot import PlanSlot
from common.store import TelemetryStore, read_telemetry
from experiments.kpis import load_carbon, load_tariff
from guardian.core import Guardian as CoreGuardian
from guardian.executor import Executor
from simulation.run_baseline import _read_epw_drybulb

log = get_logger("experiments.smoke_llm")

_HISTORY = 6  # keep the last few observations for the digest's trend arrows


class StateHolder:
    """Thread-safe capture of recent building states, written on the callback thread."""

    def __init__(self, maxlen: int = _HISTORY) -> None:
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


@dataclass(frozen=True)
class LlmLoopResult:
    plans_made: int
    accepted: int
    clipped: int
    timeouts: int
    timesteps: int
    setpoint_moved: bool

    @property
    def ok(self) -> bool:
        return self.timesteps > 0 and self.accepted >= 1 and self.setpoint_moved

    def summary(self) -> str:
        # The mandated 5-line scoreboard.
        return (
            f"plans made : {self.plans_made}\n"
            f"accepted   : {self.accepted}\n"
            f"clipped    : {self.clipped}\n"
            f"timeouts   : {self.timeouts}\n"
            f"cache      : n/a"
        )


def _baseline_map(model: PreparedModel) -> dict[tuple[str, str], float]:
    """``{(zone, actuator): baseline_value}`` for lowering plan action windows back to baseline."""
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


def _forecast_loader(settings: Settings):
    """Load EPW + tariff + carbon once; return a ``(now) -> list[ForecastRow]`` closure."""
    epw = _read_epw_drybulb(settings.epw_path)
    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")

    def forecast(now: datetime) -> list[ForecastRow]:
        return load_forecast(now, epw_drybulb=epw, tariff=tariff, carbon=carbon, hours=6)

    return forecast


def run_llm_loop(
    settings: Settings | None = None,
    *,
    db_path: Path | None = None,
    timeout_s: float = 30.0,
) -> LlmLoopResult:
    """Run one simulated day with the live planner in the loop."""
    settings = settings or Settings.from_env()
    eplus_path.require_energyplus()

    index_path = settings.simulation_dir / "agentic_model.json"
    if not index_path.is_file():
        raise FileNotFoundError(
            f"{index_path} not found. Run `python -m simulation.prepare_idf` first."
        )
    model = PreparedModel.load(index_path)

    db_path = db_path or (settings.repo_root / "hive_llm.sqlite")
    if db_path.exists():
        db_path.unlink()

    run_id = "smoke-llm"
    holder = StateHolder()
    forecast = _forecast_loader(settings)
    plan_slot = PlanSlot()

    with TelemetryStore(db_path, flush_every_timesteps=12) as store:
        planner = Planner(
            model=settings.ollama_model,
            host=settings.ollama_host,
            timeout_s=timeout_s,
            keep_alive=settings.ollama_keep_alive,
            store=store,
            run_id=run_id,
        )
        if not planner.health():
            log.warning("Ollama/model not reachable at %s - plans will fail", settings.ollama_host)

        def digest_provider(now: datetime) -> str:
            state = holder.latest()
            if state is None:
                return "SIM TIME: (no observation yet)"
            return build_digest(
                state,
                history=holder.history(),
                forecast=forecast(now),
                active_plan=plan_slot.get(),
                feedback=None,  # Session 9 fills this from the guardian's reasons
            )

        scheduler = Scheduler(
            planner=planner,
            plan_slot=plan_slot,
            digest_provider=digest_provider,
            baseline=_baseline_map(model),
            timeout_s=timeout_s,
            store=store,
            run_id=run_id,
        )
        executor = Executor(
            guardian=CoreGuardian(),
            model=model,
            plan_slot=plan_slot,
            plan_interval=timedelta(minutes=settings.plan_interval_minutes),
            run_id=run_id,
        )
        bus = SimulationBus(
            model=model,
            store=store,
            run_id=run_id,
            epw_path=settings.epw_path,
            out_dir=settings.simulation_dir / "out_smoke_llm",
        )
        executor.control = bus

        def provider(now: datetime):
            # Runs on the callback thread. Capture state, poke the scheduler (non-blocking),
            # then let the executor apply whatever plan is currently in the slot.
            state = bus.read_state()
            if state is not None:
                holder.push(state)
                scheduler.on_timestep(now, state)
            return executor.provide(now)

        bus.plan_provider = provider

        exit_code = bus.run()
        scheduler.join(timeout=max(2.0, timeout_s))
        executor.drain_events(store, run_id=run_id)

    log.info(
        "llm loop: exit=%s | scheduler %s | executor %s",
        exit_code,
        scheduler.stats.summary(),
        executor.stats.summary(),
    )

    setpoint_moved = _setpoints_moved(db_path, run_id, model)
    return LlmLoopResult(
        plans_made=scheduler.stats.plans_made,
        accepted=scheduler.stats.accepted,
        clipped=executor.stats.clipped_steps,
        timeouts=scheduler.stats.timeouts,
        timesteps=bus.stats.timesteps,
        setpoint_moved=setpoint_moved,
    )


def _setpoints_moved(db_path: Path, run_id: str, model: PreparedModel) -> bool:
    """True if any cooling setpoint in telemetry departed from its baseline - proof of actuation."""
    frame = read_telemetry(db_path, run_id=run_id)
    if frame.empty or "cooling_setpoint_c" not in frame:
        return False
    baseline = _baseline_map(model)
    for binding in model.zones:
        base = baseline.get((binding.zone, Actuator.COOLING_SETPOINT_C.value))
        if base is None:
            continue
        zone_rows = frame[frame["zone"] == binding.zone]["cooling_setpoint_c"].dropna()
        if not zone_rows.empty and (zone_rows - base).abs().max() > 0.25:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live one-day LLM control-loop smoke.")
    parser.add_argument("--db", default=None, help="telemetry database path")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="planner timeout in seconds; use 0.1 to force every cycle to time out",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    result = run_llm_loop(
        settings, db_path=Path(args.db) if args.db else None, timeout_s=args.timeout
    )

    forced = args.timeout <= 1.0
    print("\n" + "=" * 40)
    print(result.summary())
    print("=" * 40)
    if forced:
        # With a sub-second budget every cycle is preempted; the day must still complete on
        # baseline. "accepted 0, sim completed" is the pass condition for this path.
        ok = result.timesteps > 0 and result.accepted == 0
        print(f"[{'PASS' if ok else 'FAIL'}] forced-timeout: sim completed on baseline, no stall")
        return 0 if ok else 1
    print(f"[{'PASS' if result.ok else 'FAIL'}] >=1 plan accepted and actuated; setpoints moved")
    return 0 if result.ok else 1


__all__ = ["LlmLoopResult", "StateHolder", "run_llm_loop"]


if __name__ == "__main__":
    raise SystemExit(main())
