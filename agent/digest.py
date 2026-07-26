"""The state digest handed to the planner.

A deliberately small (<= ~1.5K token) text block: per-zone temperature / PMV / occupancy with
one-hour trend arrows, the next six hours of weather, tariff and grid-carbon *bands* (not raw
numbers - the planner reasons about shape, not decimals), a one-line summary of the active plan,
and a ``PREVIOUS PLAN FEEDBACK`` section that is empty until Session 9 wires the guardian's
reasons back in.

Two properties matter as much as the content:

* **Deterministic ordering.** Zones in a fixed order, forecast hours ascending, fields in a
  fixed layout. Two identical states must produce byte-identical digests, or the prompt-prefix
  cache and any later diffing break.
* **Terse fixed vocabulary.** Arrows are ``up``/``down``/``flat``; bands are ``low``/``mid``/
  ``high``. A small closed vocabulary is easier for a 7B model to reason over than free text and
  keeps the token budget down.

Everything here is pure and string-only, so it is trivially exercised without EnergyPlus or a
model. The EPW/CSV reading that feeds it lives in :func:`load_forecast`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from common.models import BuildingState, Plan, SetpointPlan

#: ~4 chars/token is a good rough estimate for English + numbers; used only for the budget warning.
_CHARS_PER_TOKEN = 4
TOKEN_BUDGET = 1500

#: A zone temperature must move more than this over the hour to count as a trend, not noise.
_TREND_DEADBAND_C = 0.2


def trend_arrow(earlier: float | None, later: float | None, *, deadband: float) -> str:
    """``up`` / ``down`` / ``flat`` for a value's movement, with a noise deadband."""
    if earlier is None or later is None:
        return "flat"
    delta = later - earlier
    if delta > deadband:
        return "up"
    if delta < -deadband:
        return "down"
    return "flat"


def band(value: float | None, low_max: float, high_min: float) -> str:
    """Classify a value into ``low`` / ``mid`` / ``high`` against two thresholds."""
    if value is None:
        return "n/a"
    if value <= low_max:
        return "low"
    if value >= high_min:
        return "high"
    return "mid"


def terciles(curve: dict[int, float]) -> tuple[float, float]:
    """Return ``(low_max, high_min)`` tercile thresholds for an hour->value curve."""
    values = sorted(curve.values())
    if not values:
        return (0.0, 0.0)
    lo = values[len(values) // 3]
    hi = values[(2 * len(values)) // 3]
    return (lo, hi)


# --------------------------------------------------------------------------------------
# Forecast rows (bands precomputed, so the digest is a pure renderer)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastRow:
    at: datetime
    outdoor_c: float | None
    tariff_band: str
    carbon_band: str


def load_forecast(
    sim_time: datetime,
    *,
    epw_drybulb: list[float],
    tariff: dict[int, float],
    carbon: dict[int, float],
    hours: int = 6,
) -> list[ForecastRow]:
    """Build the next ``hours`` of forecast rows from an EPW series and the ToU/carbon curves.

    ``epw_drybulb`` is the chronological 8760-hour series (see
    ``simulation.run_baseline._read_epw_drybulb``); tariff/carbon are hour-of-day curves
    (see ``experiments.kpis.load_tariff`` / ``load_carbon``). Bands are classified by each
    curve's own terciles, so "high" means "high for this building's tariff", not an absolute.
    """
    tariff_lo, tariff_hi = terciles(tariff)
    carbon_lo, carbon_hi = terciles(carbon)
    hour_of_year = sim_time.timetuple().tm_yday * 24 + sim_time.hour - 24  # 0-based index

    rows: list[ForecastRow] = []
    for step in range(1, hours + 1):
        when = sim_time + timedelta(hours=step)
        hod = when.hour
        idx = hour_of_year + step
        outdoor = epw_drybulb[idx] if 0 <= idx < len(epw_drybulb) else None
        rows.append(
            ForecastRow(
                at=when,
                outdoor_c=outdoor,
                tariff_band=band(tariff.get(hod), tariff_lo, tariff_hi),
                carbon_band=band(carbon.get(hod), carbon_lo, carbon_hi),
            )
        )
    return rows


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _fmt(value: float | None, spec: str = ".1f") -> str:
    return format(value, spec) if value is not None else "n/a"


def _zone_lines(state: BuildingState, history: list[BuildingState]) -> list[str]:
    earliest = history[0] if history else None
    earlier_by_zone = {z.zone: z for z in earliest.zones} if earliest else {}

    lines = ["ZONES (temp / trend / pmv / occ / clg-sp / htg-sp):"]
    for zone in sorted(state.zones, key=lambda z: z.zone):
        prev = earlier_by_zone.get(zone.zone)
        arrow = trend_arrow(
            prev.air_temp_c if prev else None, zone.air_temp_c, deadband=_TREND_DEADBAND_C
        )
        lines.append(
            f"  {zone.zone:<16} {_fmt(zone.air_temp_c)}C {arrow:<4} "
            f"pmv {_fmt(zone.pmv, '+.2f')} occ {_fmt(zone.occupancy, '.0f')} "
            f"clg {_fmt(zone.cooling_setpoint_c)} htg {_fmt(zone.heating_setpoint_c)}"
        )
    return lines


def _forecast_lines(forecast: list[ForecastRow]) -> list[str]:
    lines = ["FORECAST next 6h (hour: outdoor / tariff / carbon):"]
    for row in forecast:
        lines.append(
            f"  {row.at:%H:%M}  {_fmt(row.outdoor_c, '.0f')}C / "
            f"{row.tariff_band} / {row.carbon_band}"
        )
    return lines


def _active_plan_line(active_plan: Plan | SetpointPlan | None) -> str:
    if active_plan is None:
        return "ACTIVE PLAN: none (baseline)"
    if isinstance(active_plan, Plan):
        ecms = ",".join(e.value for e in active_plan.ecms) or "none"
        return (
            f"ACTIVE PLAN: {ecms} | {len(active_plan.actions)} action(s) | "
            f"issued {active_plan.created_at:%H:%M}"
        )
    return (
        f"ACTIVE PLAN: {len(active_plan.steps)} setpoint move(s) | "
        f"issued {active_plan.created_at:%H:%M}"
    )


def build_digest(
    state: BuildingState,
    *,
    history: list[BuildingState] | None = None,
    forecast: list[ForecastRow] | None = None,
    active_plan: Plan | SetpointPlan | None = None,
    feedback: list[str] | None = None,
) -> str:
    """Render the full digest string. Deterministic for identical inputs."""
    history = history or []
    forecast = forecast or []

    occupied = any((z.occupancy or 0) > 0 for z in state.zones)
    outdoor_arrow = trend_arrow(
        history[0].outdoor_air_temp_c if history else None,
        state.outdoor_air_temp_c,
        deadband=_TREND_DEADBAND_C,
    )

    lines = [
        f"SIM TIME: {state.sim_time:%Y-%m-%d %H:%M} ({'occupied' if occupied else 'unoccupied'})",
        f"OUTDOOR: {_fmt(state.outdoor_air_temp_c)}C {outdoor_arrow} | "
        f"facility {_fmt(state.facility_power_w, '.0f')} W",
        "",
        *_zone_lines(state, history),
        "",
        *_forecast_lines(forecast),
        "",
        _active_plan_line(active_plan),
        "",
        "PREVIOUS PLAN FEEDBACK:",
    ]
    if feedback:
        lines.extend(f"  {line}" for line in feedback)
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Rough token count for the budget check."""
    return len(text) // _CHARS_PER_TOKEN


def within_budget(text: str, budget: int = TOKEN_BUDGET) -> bool:
    return estimate_tokens(text) <= budget


__all__ = [
    "TOKEN_BUDGET",
    "ForecastRow",
    "band",
    "build_digest",
    "estimate_tokens",
    "load_forecast",
    "terciles",
    "trend_arrow",
    "within_budget",
]
