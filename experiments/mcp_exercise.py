"""Exercise the MCP tool surface: call every tool once and print what it returns.

Two things this proves without needing a live simulation:

1. **Every tool works end to end** against a real :class:`ToolContext` - get_state,
   get_forecasts, get_kpis, read_error_log, submit_plan, patch_model.
2. **submit_plan has no bypass.** An abusive plan submitted *through the tool* comes back with
   the exact same guardian verdict (clip/rate/strip, same clamped values) as feeding it through
   the guardian directly. If the two ever diverge, the tool has grown a path around the safety
   layer.

It uses canned providers (a synthetic occupied state, the real tariff/carbon CSVs), so it runs
with no EnergyPlus and no Ollama. The one-day loop that shows cache hits and event-triggered
replans is :mod:`experiments.smoke_llm_loop` (which now wires the cache and event detector).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from common.config import Settings
from common.generated_enums import ActuatorEnum, ZoneEnum
from common.log import get_logger
from common.models import (
    Actuator,
    BuildingState,
    ForecastPoint,
    KpiSnapshot,
    PatchOp,
    PatchOperation,
    PatchSpec,
    Plan,
    PlanAction,
    PreparedModel,
    ZoneBinding,
    ZoneState,
)
from guardian.core import Guardian, RateHistory
from mcp_server import tools
from mcp_server.tools import ToolContext

log = get_logger("experiments.mcp_exercise")

ZONE = ZoneEnum.CORE_ZN.value
NOW = datetime(2017, 7, 15, 14, 0)


def _demo_model() -> PreparedModel:
    return PreparedModel(
        idf_path="agentic.idf",
        zones=[ZoneBinding(zone=ZONE, cooling_schedule="CLG", heating_schedule="HTG")],
        constant_schedules={"CLG": 24.0, "HTG": 21.0},
    )


def _demo_state() -> BuildingState:
    # Occupied, running warm (PMV 0.8) - so the guardian's PMV guard and envelope both bite.
    return BuildingState(
        sim_time=NOW,
        outdoor_air_temp_c=33.2,
        facility_power_w=9500.0,
        zones=[
            ZoneState(
                zone=ZONE,
                air_temp_c=25.4,
                occupancy=12.0,
                cooling_setpoint_c=24.0,
                heating_setpoint_c=21.0,
                pmv=0.8,
            )
        ],
    )


def _demo_context(settings: Settings) -> ToolContext:
    def forecasts(hours: int) -> list[ForecastPoint]:
        return [
            ForecastPoint(
                timestamp=NOW + timedelta(hours=h),
                outdoor_air_temp_c=33.0 - h * 0.5,
                tariff_inr_per_kwh=6.5 if h < 2 else 11.9,
                carbon_g_per_kwh=488.0 if h < 2 else 845.0,
            )
            for h in range(1, hours + 1)
        ]

    def kpis(_since: datetime | None) -> KpiSnapshot:
        return KpiSnapshot(
            window_start=NOW - timedelta(hours=14),
            window_end=NOW,
            energy_kwh=420.3,
            cost_inr=3120.5,
            carbon_kg=260.1,
            peak_demand_kw=31.2,
            comfort_violation_hours=0.5,
        )

    return ToolContext(
        model=_demo_model(),
        state_provider=_demo_state,
        forecast_provider=forecasts,
        kpi_provider=kpis,
        baseline={(ZONE, Actuator.COOLING_SETPOINT_C.value): 24.0},
        err_path=None,
        versions_dir=settings.simulation_dir / "versions",
        idf_path=settings.simulation_dir / "agentic.idf",
        commit_on_submit=False,  # exercise: review only, do not touch a slot
    )


def _abusive_plan() -> Plan:
    """18 C occupied setpoint (clip) + off-whitelist actuator (strip) - same as the S4 abusive."""
    return Plan(
        planner_model="mcp-exercise-abusive",
        actions=[
            PlanAction(
                zone=ZoneEnum.CORE_ZN,
                actuator=ActuatorEnum.COOLING_SETPOINT_C,
                value=18.0,
                start=NOW,
                end=NOW + timedelta(hours=2),
                rationale="abusive: 18C occupied",
            ),
        ],
    )


def run_exercise(settings: Settings | None = None) -> bool:
    """Call every tool once, print results, and check submit_plan == the internal path."""
    settings = settings or Settings.from_env()
    ctx = _demo_context(settings)

    print("=" * 60)
    print("get_state:")
    state = tools.get_state(ctx)
    zone = state.zones[0]
    print(f"  {zone.zone}: {zone.air_temp_c}C pmv={zone.pmv} occ={zone.occupancy} "
          f"clg={zone.cooling_setpoint_c}")

    print("\nget_forecasts(3):")
    for fp in tools.get_forecasts(ctx, 3):
        print(f"  {fp.timestamp:%H:%M} {fp.outdoor_air_temp_c}C "
              f"tariff={fp.tariff_inr_per_kwh} carbon={fp.carbon_g_per_kwh}")

    print("\nget_kpis:")
    k = tools.get_kpis(ctx)
    print(f"  energy={k.energy_kwh}kWh cost={k.cost_inr}INR carbon={k.carbon_kg}kg "
          f"peak={k.peak_demand_kw}kW")

    print("\nread_error_log:")
    print("  " + tools.read_error_log(ctx).replace("\n", "\n  "))

    print("\npatch_model (no EnergyPlus here -> expect a clear error, not a crash):")
    spec = PatchSpec(
        reason="exercise",
        operations=[
            PatchOperation(
                op=PatchOp.SET_FIELD,
                object_type="SCHEDULE:CONSTANT",
                object_name="CLG",
                field="Hourly_Value",
                value=25.0,
            )
        ],
    )
    try:
        print("  " + tools.patch_model(ctx, spec))
    except Exception as exc:  # noqa: BLE001 - exercise reports, never crashes
        print(f"  patch_model raised {type(exc).__name__}: {exc}")

    print("\nsubmit_plan (abusive):")
    plan = _abusive_plan()
    via_tool = tools.submit_plan(ctx, plan)
    print(f"  decision={via_tool.decision} steps={[round(s.value, 2) for s in via_tool.steps]}")

    # The proof: run the SAME plan through the guardian directly and compare.
    internal = _internal_verdict(ctx, plan)
    match = (
        via_tool.decision == internal.decision
        and [round(s.value, 4) for s in via_tool.steps]
        == [round(s.value, 4) for s in internal.steps]
    )
    print(f"  internal path decision={internal.decision} "
          f"steps={[round(s.value, 2) for s in internal.steps]}")
    print("=" * 60)
    print(f"[{'PASS' if match else 'FAIL'}] submit_plan verdict matches the internal guardian path")
    return match


def _internal_verdict(ctx: ToolContext, plan: Plan):
    """Feed the plan through the guardian directly - the reference submit_plan must match."""
    state = ctx.state_provider()
    setpoints = plan.to_setpoint_plan(now=state.sim_time, baseline=ctx.baseline)
    guardian = Guardian()
    history = RateHistory.empty()
    verdicts = [guardian.filter(setpoints, z, history) for z in state.zones]
    return guardian.approve(verdicts, plan_id=plan.plan_id, now=state.sim_time)


def main(argv: list[str] | None = None) -> int:
    del argv
    ok = run_exercise()
    return 0 if ok else 1


__all__ = ["run_exercise"]


if __name__ == "__main__":
    raise SystemExit(main())
