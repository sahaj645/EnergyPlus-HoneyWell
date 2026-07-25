"""KPI extraction from an EnergyPlus run.

Source of truth is the EnergyPlus **SQLite** output (``eplusout.sql``), not the CSV/ESO
tables - it is the most robust and unambiguous of the outputs, and the meter values in it are
interval energy in joules, which is all the arithmetic here needs.

What this produces, for one run:

* total site electricity (kWh),
* HVAC subsystem electricity, itemised (cooling / fans / pumps),
* peak electrical demand (kW),
* cost (INR) and carbon (kgCO2), by joining each interval's kWh against
  ``data/tariff.csv`` and ``data/carbon_intensity.csv`` on the interval's clock hour.

Three correctness points, all exercised by ``tests/test_kpis.py``:

* **Warmup is excluded.** EnergyPlus reports some rows during warmup; ``Time.WarmupFlag`` marks
  them and they are dropped.
* **Only the weather run period counts.** Design-day environments are dropped via
  ``EnvironmentPeriods.EnvironmentType``; anything outside the RunPeriod does not contribute.
* **The timestep is inferred, never hardcoded.** ``Time.Interval`` carries the interval length
  in minutes; peak demand uses each interval's own length.

Not to be confused with :mod:`experiments.kpi_extract`, which reads the *HIVE* telemetry
database written during agentic runs. This module reads EnergyPlus' own SQL - the only source
that exists for the no-agent baseline.

CLI::

    python -m experiments.kpis simulation/out_baseline
    python -m experiments.kpis simulation/out_baseline/eplusout.sql --json reports/kpis.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from common.config import Settings
from common.log import get_logger

log = get_logger("experiments.kpis")

J_PER_KWH = 3_600_000.0

#: Facility electricity meter - the basis for site energy, peak, cost and carbon.
SITE_METER = "Electricity:Facility"

#: HVAC subsystem meters, itemised. Order here is the order shown in the table.
HVAC_METERS: dict[str, str] = {
    "cooling": "Cooling:Electricity",
    "fans": "Fans:Electricity",
    "pumps": "Pumps:Electricity",
}

#: EnergyPlus ``EnvironmentPeriods.EnvironmentType`` code for a weather-file run period.
_ENV_TYPE_WEATHER_RUN_PERIOD = 3


# --------------------------------------------------------------------------------------
# Result record
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Kpis:
    """Extracted KPIs for a single EnergyPlus run.

    This is deliberately a plain dataclass and not :class:`common.models.KpiSnapshot`: it
    carries the baseline-extraction detail (itemised HVAC, inferred timestep, interval count)
    that the run-time scoreboard model does not. It can be projected onto a ``KpiSnapshot``
    when one is needed; it never replaces it.
    """

    run_label: str
    sql_path: str
    timestep_minutes: int
    intervals_counted: int
    site_kwh: float
    hvac_kwh: float
    hvac_breakdown_kwh: dict[str, float] = field(default_factory=dict)
    peak_demand_kw: float = 0.0
    cost_inr: float = 0.0
    carbon_kg: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: Path | str | None = None, *, indent: int = 2) -> str:
        """Serialise to JSON. Writes to ``path`` if given; always returns the JSON string."""
        text = json.dumps(self.to_dict(), indent=indent, sort_keys=True)
        if path is not None:
            out = Path(path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
        return text


@dataclass(frozen=True)
class _Interval:
    """One meter reading over one reporting interval."""

    meter: str
    hour: int  # clock hour (0-23) at the interval midpoint
    interval_min: int
    value_j: float


# --------------------------------------------------------------------------------------
# Price and carbon curves
# --------------------------------------------------------------------------------------


def load_tariff(path: Path | str) -> dict[int, float]:
    """Load the ToU tariff as ``{hour: inr_per_kwh}`` for all 24 hours."""
    frame = pd.read_csv(path, comment="#")
    table = {int(h): float(v) for h, v in zip(frame["hour"], frame["inr_per_kwh"], strict=True)}
    _require_full_day(table, path, "tariff")
    return table


def load_carbon(path: Path | str) -> dict[int, float]:
    """Load grid carbon intensity as ``{hour: g_co2_per_kwh}`` for all 24 hours."""
    frame = pd.read_csv(path, comment="#")
    table = {int(h): float(v) for h, v in zip(frame["hour"], frame["g_co2_per_kwh"], strict=True)}
    _require_full_day(table, path, "carbon")
    return table


def _require_full_day(table: dict[int, float], path: Path | str, what: str) -> None:
    missing = [h for h in range(24) if h not in table]
    if missing:
        raise ValueError(f"{what} curve {path} is missing hours {missing}; need all of 0-23")


# --------------------------------------------------------------------------------------
# EnergyPlus SQL reading
# --------------------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _run_period_env_indices(conn: sqlite3.Connection) -> set[int] | None:
    """Return the environment indices that are weather run periods.

    ``None`` means "do not filter by environment" - either the table is absent or it lists no
    weather run period (in which case warmup exclusion is the only defence we have).
    """
    if not _table_exists(conn, "EnvironmentPeriods"):
        return None
    rows = conn.execute(
        "SELECT EnvironmentPeriodIndex AS idx, EnvironmentType AS kind FROM EnvironmentPeriods"
    ).fetchall()
    weather = {int(r["idx"]) for r in rows if int(r["kind"]) == _ENV_TYPE_WEATHER_RUN_PERIOD}
    return weather or None


def _midpoint_hour(
    year: int, month: int, day: int, hour: int, minute: int, interval_min: int
) -> int:
    """Clock hour (0-23) at the midpoint of an interval whose *end* is the E+ timestamp.

    EnergyPlus timestamps the end of the interval and uses ``Hour`` 1-24, so the end can be
    24:00. Building the end via ``timedelta`` from the day start sidesteps the ``hour==24``
    problem, and the midpoint keeps a sub-hourly interval firmly inside one clock hour.
    """
    base_year = int(year) if year and int(year) >= 1 else 2017
    day_start = datetime(base_year, int(month), int(day))
    end = day_start + timedelta(hours=int(hour), minutes=int(minute))
    midpoint = end - timedelta(minutes=interval_min / 2.0)
    return midpoint.hour


def _read_intervals(
    conn: sqlite3.Connection,
    meters: list[str],
    env_indices: set[int] | None,
) -> list[_Interval]:
    """Read meter intervals, excluding warmup and non-run-period environments."""
    placeholders = ",".join("?" for _ in meters)
    sql = f"""
        SELECT rdd.Name AS meter,
               rd.Value AS value_j,
               t.Year AS year, t.Month AS month, t.Day AS day,
               t.Hour AS hour, t.Minute AS minute, t.Interval AS interval_min,
               t.EnvironmentPeriodIndex AS env_idx, t.WarmupFlag AS warmup
        FROM ReportData rd
        JOIN ReportDataDictionary rdd
             ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
        JOIN Time t ON rd.TimeIndex = t.TimeIndex
        WHERE rdd.Name IN ({placeholders})
          AND (t.WarmupFlag = 0 OR t.WarmupFlag IS NULL)
    """
    rows = conn.execute(sql, tuple(meters)).fetchall()

    intervals: list[_Interval] = []
    for r in rows:
        env = r["env_idx"]
        if env_indices is not None and env is not None and int(env) not in env_indices:
            continue
        raw_interval = r["interval_min"]
        interval_min = int(raw_interval) if raw_interval not in (None, 0) else 0
        hour = _midpoint_hour(
            r["year"], r["month"], r["day"], r["hour"], r["minute"], interval_min or 60
        )
        intervals.append(
            _Interval(
                meter=r["meter"], hour=hour, interval_min=interval_min, value_j=float(r["value_j"])
            )
        )
    return intervals


def _infer_timestep_minutes(intervals: list[_Interval]) -> int:
    """Most common interval length among the readings. Defaults to 60 if none are known."""
    known = [iv.interval_min for iv in intervals if iv.interval_min > 0]
    if not known:
        return 60
    return Counter(known).most_common(1)[0][0]


# --------------------------------------------------------------------------------------
# Aggregation (pure - the unit under test)
# --------------------------------------------------------------------------------------


def aggregate_site(
    site_intervals: list[_Interval],
    tariff: dict[int, float],
    carbon: dict[int, float],
) -> tuple[float, float, float, float]:
    """Energy, peak, cost and carbon for the facility meter.

    Returns ``(energy_kwh, peak_kw, cost_inr, carbon_kg)``. This is the join being verified:
    each interval's kWh is priced and carbon-weighted by its clock hour.
    """
    energy_kwh = 0.0
    peak_kw = 0.0
    cost_inr = 0.0
    carbon_g = 0.0
    for iv in site_intervals:
        kwh = iv.value_j / J_PER_KWH
        energy_kwh += kwh
        if iv.interval_min > 0:
            peak_kw = max(peak_kw, kwh / (iv.interval_min / 60.0))
        cost_inr += kwh * tariff[iv.hour]
        carbon_g += kwh * carbon[iv.hour]
    return energy_kwh, peak_kw, cost_inr, carbon_g / 1000.0


# --------------------------------------------------------------------------------------
# Top-level extraction
# --------------------------------------------------------------------------------------


def find_sql(path: Path | str) -> Path:
    """Resolve a run directory or file to the ``eplusout.sql`` inside it."""
    p = Path(path)
    if p.is_file():
        return p
    if p.is_dir():
        preferred = p / "eplusout.sql"
        if preferred.is_file():
            return preferred
        candidates = sorted(p.glob("*.sql"))
        if candidates:
            return candidates[0]
    raise FileNotFoundError(
        f"no EnergyPlus SQL output found at {p}. Expected eplusout.sql - was the run "
        "configured with `Output:SQLite`? (run_baseline patches it in.)"
    )


def compute_kpis(
    sql_path: Path | str,
    tariff_path: Path | str,
    carbon_path: Path | str,
    run_label: str = "baseline",
) -> Kpis:
    """Extract KPIs from one EnergyPlus SQL file."""
    sql_file = find_sql(sql_path)
    tariff = load_tariff(tariff_path)
    carbon = load_carbon(carbon_path)

    meters = [SITE_METER, *HVAC_METERS.values()]

    conn = sqlite3.connect(str(sql_file))
    conn.row_factory = sqlite3.Row
    try:
        env_indices = _run_period_env_indices(conn)
        intervals = _read_intervals(conn, meters, env_indices)
    finally:
        conn.close()

    timestep = _infer_timestep_minutes(intervals)
    # Fill any unknown per-row interval with the inferred timestep so peak demand is defined.
    intervals = [
        iv if iv.interval_min > 0 else replace(iv, interval_min=timestep) for iv in intervals
    ]

    site_intervals = [iv for iv in intervals if iv.meter == SITE_METER]
    site_kwh, peak_kw, cost_inr, carbon_kg = aggregate_site(site_intervals, tariff, carbon)

    breakdown = {
        label: sum(iv.value_j for iv in intervals if iv.meter == meter) / J_PER_KWH
        for label, meter in HVAC_METERS.items()
    }
    hvac_kwh = sum(breakdown.values())

    if not site_intervals:
        log.warning(
            "no '%s' rows found in %s - is the meter requested in the IDF?", SITE_METER, sql_file
        )

    return Kpis(
        run_label=run_label,
        sql_path=str(sql_file),
        timestep_minutes=timestep,
        intervals_counted=len(site_intervals),
        site_kwh=site_kwh,
        hvac_kwh=hvac_kwh,
        hvac_breakdown_kwh=breakdown,
        peak_demand_kw=peak_kw,
        cost_inr=cost_inr,
        carbon_kg=carbon_kg,
    )


def format_table(kpis: Kpis) -> str:
    """Render KPIs as a fixed-width table for the terminal."""
    lines = [
        f"KPIs - {kpis.run_label}",
        f"  source            {kpis.sql_path}",
        f"  timestep          {kpis.timestep_minutes} min  ({kpis.intervals_counted} intervals)",
        "  " + "-" * 44,
        f"  site electricity  {kpis.site_kwh:12.2f} kWh",
        f"  HVAC total        {kpis.hvac_kwh:12.2f} kWh",
    ]
    for label, value in kpis.hvac_breakdown_kwh.items():
        lines.append(f"    - {label:<13} {value:12.2f} kWh")
    lines.extend(
        [
            f"  peak demand       {kpis.peak_demand_kw:12.2f} kW",
            f"  cost (ToU)        {kpis.cost_inr:12.2f} INR",
            f"  carbon            {kpis.carbon_kg:12.2f} kgCO2",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Extract KPIs from an EnergyPlus SQL run.")
    parser.add_argument("run", help="run output directory or path to eplusout.sql")
    parser.add_argument("--tariff", default=str(settings.data_dir / "tariff.csv"))
    parser.add_argument("--carbon", default=str(settings.data_dir / "carbon_intensity.csv"))
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--json", default=None, help="also write the KPIs to this JSON path")
    args = parser.parse_args(argv)

    kpis = compute_kpis(args.run, args.tariff, args.carbon, run_label=args.label)
    print(format_table(kpis))
    if args.json:
        kpis.to_json(args.json)
        log.info("wrote %s", args.json)
    return 0


__all__ = [
    "HVAC_METERS",
    "SITE_METER",
    "Kpis",
    "aggregate_site",
    "compute_kpis",
    "find_sql",
    "format_table",
    "load_carbon",
    "load_tariff",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
