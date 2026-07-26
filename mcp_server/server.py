"""MCP stdio server exposing HIVE's tool surface to an LLM planner.

The ``mcp`` SDK is imported lazily inside :func:`build_server`, so importing this module never
requires the SDK or opens a transport - the tests and the exercise script import it freely.

The tool *implementations* live in :mod:`mcp_server.tools` (pure, SDK-free). Here they are
wrapped as MCP tools whose docstrings are written **for the model that will call them**: exact
shapes, units, and one worked example each. FastMCP turns the type hints into the input schema
and the return Pydantic models into the output schema.

The safety invariant is structural: ``submit_plan`` is the only mutating-ish tool for control,
and it returns a guardian *verdict* - it cannot actuate. There is no tool, argument, or flag
that writes an actuator without the guardian.
"""

from __future__ import annotations

from datetime import datetime

from common.log import get_logger
from common.models import ApprovedPlan, BuildingState, ForecastPoint, KpiSnapshot, PatchSpec, Plan
from mcp_server import tools
from mcp_server.tools import TOOL_NAMES, ToolContext

log = get_logger("mcp_server.server")

SERVER_NAME = "hive"


def build_server(ctx: ToolContext):
    """Construct a FastMCP server with the six tools bound to ``ctx``. Imports ``mcp`` lazily."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(SERVER_NAME)

    @mcp.tool()
    def get_state() -> BuildingState:
        """Read the building's current state: one entry per conditioned zone plus site weather.

        Returns a BuildingState:
          - sim_time (ISO datetime), outdoor_air_temp_c (C), facility_power_w (W)
          - zones[]: {zone, air_temp_c (C), pmv (Fanger, ~-3..+3), occupancy (people count),
            cooling_setpoint_c (C), heating_setpoint_c (C)}

        Example: {"sim_time":"2017-07-15T14:00:00","outdoor_air_temp_c":33.2,
        "facility_power_w":9500,"zones":[{"zone":"Core_ZN","air_temp_c":24.4,"pmv":0.5,
        "occupancy":12,"cooling_setpoint_c":24.0,"heating_setpoint_c":21.0}]}
        """
        return tools.get_state(ctx)

    @mcp.tool()
    def get_forecasts(hours: int = 6) -> list[ForecastPoint]:
        """Weather, tariff and grid carbon for the next `hours` (1..24, default 6).

        Returns a list of ForecastPoint, one per hour ahead:
          - timestamp (ISO datetime)
          - outdoor_air_temp_c (C)
          - tariff_inr_per_kwh (INR/kWh)
          - carbon_g_per_kwh (gCO2/kWh)

        Use it to place precool/coast: cheap+clean midday, expensive+dirty evening peak.
        Example element: {"timestamp":"2017-07-15T15:00:00","outdoor_air_temp_c":33.0,
        "tariff_inr_per_kwh":6.5,"carbon_g_per_kwh":488.0}
        """
        return tools.get_forecasts(ctx, hours)

    @mcp.tool()
    def get_kpis(since: datetime | None = None) -> KpiSnapshot:
        """Cumulative performance for the run (optionally since an ISO datetime).

        Returns a KpiSnapshot:
          - window_start/window_end (ISO datetime)
          - energy_kwh (kWh), cost_inr (INR), carbon_kg (kgCO2)
          - peak_demand_kw (kW), comfort_violation_hours (occupied hours with |PMV|>0.5)

        Example: {"window_start":"2017-07-15T00:00:00","window_end":"2017-07-15T14:00:00",
        "energy_kwh":420.3,"cost_inr":3120.5,"carbon_kg":260.1,"peak_demand_kw":31.2,
        "comfort_violation_hours":0.5}
        """
        return tools.get_kpis(ctx, since)

    @mcp.tool()
    def read_error_log(lines: int = 50) -> str:
        """Return only Severe/Fatal lines from the EnergyPlus .err file (at most `lines`, <=50).

        Empty of errors -> "(no Severe or Fatal errors)". Use this after a patch_model to check
        you did not break the model. Example return:
        "** Severe  ** GetSurfaceData: Some Vertices are colinear ..."
        """
        return tools.read_error_log(ctx, lines)

    @mcp.tool()
    def submit_plan(plan: Plan) -> ApprovedPlan:
        """Submit a Plan for the guardian to review. Returns the verdict; NEVER actuates.

        Input Plan: {trigger, horizon_hours (4..6), ecms:[...], actions:[{zone, actuator,
        value, start, end, rationale}]}. zone/actuator must be from get_state's zones and the
        allowed actuators (cooling_setpoint_c, heating_setpoint_c). start/end are ISO datetimes
        within the horizon.

        Returns an ApprovedPlan verdict:
          - decision: "accepted" | "clamped" | "rejected"
          - steps[]: the setpoints the guardian will allow (may be clamped from yours)
          - the guardian clamps to the comfort envelope, rate-limits big jumps, and strips
            unknown actuators. A rejected plan means the building holds baseline.

        Example input action: {"zone":"Core_ZN","actuator":"cooling_setpoint_c","value":22.5,
        "start":"2017-07-15T14:00:00","end":"2017-07-15T16:00:00","rationale":"precool"}
        """
        return tools.submit_plan(ctx, plan)

    @mcp.tool()
    def patch_model(spec: PatchSpec) -> str:
        """Apply a versioned IDF edit; auto-rolls back if the patched model does not parse.

        Input PatchSpec: {reason, operations:[{op, object_type, object_name, field?, value?,
        fields?}]}. op is "set_field" | "add_object" | "remove_object". Powerful and risky - it
        can change any model object. Returns "applied: vN_... " or "rejected: ...".

        Example op: {"op":"set_field","object_type":"SCHEDULE:CONSTANT",
        "object_name":"CLGSETP_SCH","field":"Hourly_Value","value":25.0}
        """
        return tools.patch_model(ctx, spec)

    return mcp


def serve(ctx: ToolContext) -> None:
    """Run the server on stdio. Blocking; call from ``__main__`` only."""
    server = build_server(ctx)
    server.run(transport="stdio")


def build_default_context() -> ToolContext:
    """Build a context wired to the default database and simulation assets, for a standalone run.

    State comes from the last telemetry row (there is no live sim attached to a standalone
    server), forecasts and KPIs from the EPW/CSVs and the DB. Missing assets degrade to empty
    providers rather than failing to import.
    """
    from common.config import Settings
    from common.models import PreparedModel
    from common.store import read_telemetry
    from mcp_server.providers import make_forecast_provider, make_kpi_provider

    settings = Settings.from_env()
    index_path = settings.simulation_dir / "agentic_model.json"
    model = PreparedModel.load(index_path) if index_path.is_file() else PreparedModel(idf_path="")

    def latest_state() -> BuildingState | None:
        frame = read_telemetry(settings.db_path)
        if frame.empty:
            return None
        last_time = frame["sim_time"].max()
        rows = frame[frame["sim_time"] == last_time]
        from common.models import ZoneState

        zones = [
            ZoneState(
                zone=r["zone"],
                air_temp_c=float(r["air_temp_c"]),
                pmv=None if r["pmv"] is None else float(r["pmv"]),
                occupancy=None if r["occupancy"] is None else float(r["occupancy"]),
                cooling_setpoint_c=None if r["cooling_setpoint_c"] is None
                else float(r["cooling_setpoint_c"]),
                heating_setpoint_c=None if r["heating_setpoint_c"] is None
                else float(r["heating_setpoint_c"]),
            )
            for _, r in rows.iterrows()
        ]
        first = rows.iloc[0]
        return BuildingState(
            sim_time=last_time.to_pydatetime(),
            outdoor_air_temp_c=float(first["outdoor_air_temp_c"]),
            facility_power_w=float(first["facility_power_w"] or 0.0),
            zones=zones,
        )

    return ToolContext(
        model=model,
        state_provider=latest_state,
        forecast_provider=make_forecast_provider(settings, lambda: _now_of(latest_state())),
        kpi_provider=make_kpi_provider(settings.db_path, settings),
        err_path=settings.simulation_dir / "out_smoke_llm" / "eplusout.err",
        versions_dir=settings.simulation_dir / "versions",
        idf_path=settings.simulation_dir / "agentic.idf",
    )


def _now_of(state: BuildingState | None) -> datetime | None:
    return state.sim_time if state is not None else None


def main() -> None:
    """CLI entry point: ``python -m mcp_server.server`` (serves on stdio)."""
    log.info("%s: serving %d tools over stdio -> %s", SERVER_NAME, len(TOOL_NAMES),
             ", ".join(TOOL_NAMES))
    serve(build_default_context())


if __name__ == "__main__":
    main()
