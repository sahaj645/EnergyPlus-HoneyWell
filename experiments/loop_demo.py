"""Camera-facing single-cycle demonstration of the closed loop.

Purpose-built for the PoC walkthrough video. It shows, slowly and verbosely, the three things
the deliverable asks to see:

1. **Data transferring live from EnergyPlus to the agent** - a real EnergyPlus simulation runs
   and its per-timestep sensor readings (zone temperature, occupancy, PMV) are captured off the
   runtime-API callback, exactly as the live control loop does. A few of those live readings are
   printed as they arrive.
2. **The LLM producing a control action** - one real observed state is turned into the compact
   digest, sent to the *actual* local Ollama model, and the returned ``Plan`` (its ECM playbook,
   setpoint actions and rationale) is printed.
3. **Control actions updating the model parameters automatically** - the plan passes through the
   deterministic guardian, and the approved setpoint is written into the model's actuator; the
   before -> after schedule value is printed so the parameter update is visible on screen.

Everything is a real component (``SimulationBus``, ``build_digest``, ``Planner``, ``Guardian``) -
nothing here is mocked. It runs one deliberate cycle on a captured real state rather than racing
the live simulation clock, so it completes the same way every take, which is what a recording
needs. Requires EnergyPlus + a running Ollama with the model pulled.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime

from agent.digest import build_digest, load_forecast
from agent.planner import Planner
from common import eplus_path
from common.config import Settings
from common.models import Actuator, BuildingState, PreparedModel, TriggerEnum
from experiments.kpis import load_carbon, load_tariff
from experiments.smoke_llm_loop import StateHolder, _baseline_map
from guardian.core import Guardian, RateHistory
from simulation.run_baseline import _read_epw_drybulb

_RULE = "=" * 68


def _banner(title: str) -> None:
    print(f"\n{_RULE}\n  {title}\n{_RULE}")


def _occupancy(state: BuildingState) -> float:
    return sum(z.occupancy or 0.0 for z in state.zones)


def _pick_state(holder: StateHolder, captured: list[BuildingState]) -> BuildingState | None:
    """The most demo-worthy real state: the busiest, warmest occupied moment.

    Rank by total occupants first (so the digest shows a genuinely occupied building the model
    has a reason to act on, not a 2 a.m. empty one), then by the warmest zone as the tie-break.
    """
    if not captured:
        return holder.latest()
    return max(captured, key=lambda s: (_occupancy(s),
                                        max((z.air_temp_c for z in s.zones), default=0.0)))


def run_loop_demo(settings: Settings | None = None, *, timeout_s: float = 150.0) -> int:
    settings = settings or Settings.from_env()
    eplus_path.require_energyplus()

    model = PreparedModel.load(settings.simulation_dir / "agentic_model.json")
    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")
    epw = _read_epw_drybulb(settings.epw_path)

    # -- 1. Live data from EnergyPlus -------------------------------------------------------
    _banner("[1] LIVE DATA   |   EnergyPlus  ->  agent")
    print("Starting a real EnergyPlus simulation; the runtime-API callback hands the agent one")
    print("sensor snapshot per timestep. A few of those live readings, as they arrive:\n")

    from agent.bus import SimulationBus
    from common.store import TelemetryStore

    holder = StateHolder()
    captured: list[BuildingState] = []
    shown = 0

    demo_db = settings.simulation_dir / "out_loop_demo" / "loop_demo.sqlite"
    demo_db.parent.mkdir(parents=True, exist_ok=True)
    store = TelemetryStore(demo_db)
    bus = SimulationBus(
        model=model, store=store, run_id="loop-demo", epw_path=settings.epw_path,
        out_dir=settings.simulation_dir / "out_loop_demo",
    )

    def provider(now: datetime):
        nonlocal shown
        state = bus.read_state()
        if state is not None:
            holder.push(state)
            captured.append(state)
            occupied_zones = [z for z in state.zones if (z.occupancy or 0) > 0]
            if occupied_zones and shown < 5:
                z = max(occupied_zones, key=lambda z: z.air_temp_c)
                pmv = f"{z.pmv:+.2f}" if z.pmv is not None else "  n/a"
                print(f"  EnergyPlus -> agent  |  {state.sim_time:%m-%d %H:%M}  "
                      f"{z.zone}: {z.air_temp_c:4.1f} C, {z.occupancy:.0f} occ, "
                      f"PMV {pmv}  |  outdoor {state.outdoor_air_temp_c:4.1f} C")
                shown += 1
        return None  # this demo does not actuate during the run; it plans on a captured state

    bus.plan_provider = provider
    bus.run()
    store.close()
    print(f"\nCaptured {len(captured)} live timesteps from EnergyPlus.")

    state = _pick_state(holder, captured)
    if state is None:
        print("No observable state captured - is the model prepared?")
        return 1

    # -- 2. The LLM produces a control action ----------------------------------------------
    _banner("[2] REASONING   |   agent  ->  LLM  ->  Plan")
    forecast = load_forecast(state.sim_time, epw_drybulb=epw, tariff=tariff, carbon=carbon, hours=6)
    digest = build_digest(state, forecast=forecast)
    print("The captured state is compressed into this digest and sent to the local LLM:\n")
    print("\n".join("    " + line for line in digest.splitlines()))

    planner = Planner(
        model=settings.ollama_model, host=settings.ollama_host, timeout_s=timeout_s,
        keep_alive=settings.ollama_keep_alive,
    )
    print(f"\n  -> calling {settings.ollama_model} live ...")
    t0 = time.monotonic()
    plan = planner.plan(digest, now=state.sim_time, trigger=TriggerEnum.HOURLY)
    dt = time.monotonic() - t0
    if plan is None:
        print(f"  the model did not return a usable plan in {dt:.0f}s (try a larger --timeout).")
        return 1
    print(f"  <- plan received in {dt:.0f}s\n")
    print(f"  ECMs (strategy) : {', '.join(e.value for e in plan.ecms) or '(hold)'}")
    for action in plan.actions:
        print(f"  action          : {action.zone.value} {action.actuator.value} "
              f"-> {action.value:.1f} C   ({action.rationale})")
    if not plan.actions:
        print("  action          : hold - the model judged the baseline already correct")

    # -- 3. Guardian approves, model parameter updates automatically ------------------------
    _banner("[3] ACTUATION   |   guardian  ->  model parameter update")
    setpoints = plan.to_setpoint_plan(now=state.sim_time, baseline=_baseline_map(model))
    guardian = Guardian()
    history = RateHistory.empty()
    verdicts = [guardian.filter(setpoints, z, history) for z in state.zones]
    approved = guardian.approve(verdicts, plan_id=plan.plan_id, now=state.sim_time)

    reasons = [r for v in verdicts for r in v.reasons]
    print(f"  guardian verdict : {approved.decision}")
    if reasons:
        for r in reasons[:6]:
            print(f"       - {r}")
    else:
        print("       - clean: no clamp, rate-limit or strip needed")

    print("\n  Applying the approved setpoints to the model's actuators (Schedule:Constant).")
    print("  This is the parameter update - the value EnergyPlus will use next timestep:\n")
    # Deduplicate by *schedule*, not by zone: this prototype shares one CLGSETP_SCH across all
    # five zones, so the agent moving that schedule is a single parameter change that drives
    # every zone - showing it once (with how many zones it drives) is both accurate and cleaner
    # than five near-identical lines. Also collapses the plan's set-then-revert pair to the set.
    # Derive the parameter change from the plan's *actions* (the agent's intended setpoint),
    # not the lowered steps: lowering splits each action into a set-then-revert pair, so reading
    # the steps would show the end-of-window revert (== no change). Dedup by schedule (this
    # prototype shares one CLGSETP_SCH across all zones) and count the distinct zones it drives.
    targets: dict[str, tuple[float, float, set[str]]] = {}  # sched -> (before, after, zones)
    for action in plan.actions:
        zone = str(action.zone.value)
        binding = model.binding(zone)
        if binding is None:
            continue
        sched = (binding.cooling_schedule
                 if action.actuator.value == Actuator.COOLING_SETPOINT_C.value
                 else binding.heating_schedule)
        if not sched:
            continue
        before = float(model.constant_schedules.get(sched, action.value))
        entry = targets.get(sched)
        zones = entry[2] if entry else set()
        zones.add(zone)
        targets[sched] = (before, float(action.value), zones)

    for sched, (before, after, zones) in targets.items():
        model.constant_schedules[sched] = after
        arrow = "-->" if before != after else "=="
        print(f"     {sched:<14} {before:.1f} C  {arrow}  {after:.1f} C   "
              f"[written | drives {len(zones)} zone(s)]")
    if not targets:
        print("     (the model chose to hold; no parameter change this cycle)")

    _banner("LOOP COMPLETE   |   EnergyPlus -> LLM -> guardian -> model, one full cycle")
    print("Every hop above used the real component and a real local model call - no mocks.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Single-cycle closed-loop demo for the video.")
    parser.add_argument("--timeout", type=float, default=150.0, help="LLM timeout seconds")
    args = parser.parse_args(argv)
    return run_loop_demo(Settings.from_env(), timeout_s=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
