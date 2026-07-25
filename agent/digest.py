"""Digest builder: compress recent history into something worth a context window.

The model does not get raw telemetry. It gets a bounded, downsampled summary - a handful of
zones, an hourly forecast, and the running KPIs. Keeping this small is what makes local
inference fast enough to plan every quarter hour.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from common.models import BuildingState, ForecastPoint, KpiSnapshot


def build_digest(
    state: BuildingState,
    forecasts: list[ForecastPoint],
    kpis: KpiSnapshot | None,
    *,
    horizon_minutes: int = 60,
) -> str:
    """Render the planner-facing digest.

    Returns the formatted user prompt body (see :mod:`agent.prompts`).
    """
    raise NotImplementedError("digest builder not implemented yet (scaffold)")


def summarise_zones(state: BuildingState, *, max_zones: int = 8) -> str:
    """Render the per-zone block, keeping the most interesting zones only."""
    raise NotImplementedError("zone summary not implemented yet (scaffold)")


def summarise_forecast(forecasts: list[ForecastPoint], *, hours: int = 12) -> str:
    """Render the forecast block: temperature, tariff and grid carbon by hour."""
    raise NotImplementedError("forecast summary not implemented yet (scaffold)")


__all__ = ["build_digest", "summarise_forecast", "summarise_zones"]
