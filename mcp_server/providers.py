"""Builders for the data providers a :class:`~mcp_server.tools.ToolContext` needs.

Kept separate from the tools (which stay pure) and the server (which stays MCP-wiring) so both
the live server and the exercise script build their context the same way. None of this imports
the ``mcp`` SDK or EnergyPlus.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from common.config import Settings
from common.models import ForecastPoint, KpiSnapshot
from common.store import read_telemetry
from experiments.kpis import load_carbon, load_tariff
from simulation.run_baseline import _read_epw_drybulb

NowProvider = Callable[[], datetime | None]


def make_forecast_provider(
    settings: Settings, now_provider: NowProvider
) -> Callable[[int], list[ForecastPoint]]:
    """``(hours) -> [ForecastPoint]`` from the EPW series and the tariff/carbon curves.

    ``now`` comes from ``now_provider`` (usually the latest observed sim time), so forecasts are
    always relative to where the simulation actually is.
    """
    epw = _read_epw_drybulb(settings.epw_path) if settings.epw_path.is_file() else []
    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")

    def provider(hours: int) -> list[ForecastPoint]:
        now = now_provider()
        if now is None:
            return []
        hour_of_year = now.timetuple().tm_yday * 24 + now.hour - 24
        points: list[ForecastPoint] = []
        for step in range(1, hours + 1):
            when = now + timedelta(hours=step)
            hod = when.hour
            idx = hour_of_year + step
            outdoor = epw[idx] if 0 <= idx < len(epw) else None
            points.append(
                ForecastPoint(
                    timestamp=when,
                    outdoor_air_temp_c=outdoor,
                    tariff_inr_per_kwh=tariff.get(hod),
                    carbon_g_per_kwh=carbon.get(hod),
                )
            )
        return points

    return provider


def make_kpi_provider(
    db_path: Path | str,
    settings: Settings,
    *,
    run_id: str | None = None,
) -> Callable[[datetime | None], KpiSnapshot]:
    """``(since) -> KpiSnapshot`` computed from the HIVE telemetry DB.

    Energy from the per-step facility meter, cost/carbon by joining each step's kWh against the
    tariff/carbon curves on its clock hour, peak from facility power, comfort-violation hours
    from occupied zones reading ``|PMV| > 0.5``. Returns a zeroed snapshot when there is no
    telemetry yet.
    """
    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")

    def provider(since: datetime | None) -> KpiSnapshot:
        frame = read_telemetry(db_path, run_id=run_id)
        if frame.empty:
            now = since or datetime(2017, 1, 1)
            return KpiSnapshot(
                window_start=now,
                window_end=now,
                energy_kwh=0.0,
                cost_inr=0.0,
                carbon_kg=0.0,
                peak_demand_kw=0.0,
                comfort_violation_hours=0.0,
            )
        if since is not None:
            frame = frame[frame["sim_time"] >= since]

        # One facility reading per timestep (it is denormalised across zone rows).
        per_step = frame.drop_duplicates(subset=["sim_time"]).copy()
        per_step["hour"] = per_step["sim_time"].dt.hour
        per_step["kwh"] = per_step["facility_kwh_step"].fillna(0.0)
        per_step["cost"] = per_step.apply(
            lambda r: r["kwh"] * tariff.get(int(r["hour"]), 0.0), axis=1
        )
        per_step["carbon_g"] = per_step.apply(
            lambda r: r["kwh"] * carbon.get(int(r["hour"]), 0.0), axis=1
        )

        span_hours = _infer_step_hours(per_step["sim_time"])
        occupied = frame[(frame["occupancy"].fillna(0) > 0) & (frame["pmv"].abs() > 0.5)]
        comfort_hours = len(occupied) * span_hours

        peak_w = float(frame["facility_power_w"].max() or 0.0)
        return KpiSnapshot(
            window_start=frame["sim_time"].min().to_pydatetime(),
            window_end=frame["sim_time"].max().to_pydatetime(),
            energy_kwh=float(per_step["kwh"].sum()),
            cost_inr=float(per_step["cost"].sum()),
            carbon_kg=float(per_step["carbon_g"].sum()) / 1000.0,
            peak_demand_kw=peak_w / 1000.0,
            comfort_violation_hours=float(comfort_hours),
        )

    return provider


def _infer_step_hours(times) -> float:
    """Median spacing of consecutive timestamps, in hours (defaults to 10 min)."""
    if len(times) < 2:
        return 1.0 / 6.0
    deltas = times.sort_values().diff().dropna()
    if deltas.empty:
        return 1.0 / 6.0
    seconds = deltas.dt.total_seconds().median()
    return (seconds / 3600.0) if seconds and seconds > 0 else 1.0 / 6.0


__all__ = ["make_forecast_provider", "make_kpi_provider"]
