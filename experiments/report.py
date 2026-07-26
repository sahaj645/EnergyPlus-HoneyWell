"""Turn an A/B run into ``reports/results.json`` and ``reports/results.md``.

This module does not run anything - it reads the ``eplusout.sql`` each arm of
``experiments.ab`` produced and reports what is there. **No filtering logic**: every zone,
every day, every comfort excursion the run produced is printed, whether the number is
flattering or not. The only computation here is honest aggregation - sums, joins against the
tariff/carbon curves, percentage deltas - never selection.

Headline numbers (total site kWh, HVAC subsystem kWh, peak kW, cost, carbon) are computed by
:func:`experiments.kpis.compute_kpis`, already exercised by its own test suite. Everything with
a time axis - the per-day breakdown, the cumulative-kWh series for the dashboard's race chart,
and the comfort-violation table - needs full timestamps that ``kpis.Kpis`` does not carry, so
those are read here directly, with the same warmup and run-period filtering
:mod:`experiments.kpis` uses (reused, not re-derived, so the two never quietly disagree about
which rows count).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from common.config import Settings
from common.log import get_logger
from common.models import PreparedModel
from experiments.kpis import (
    HVAC_METERS,
    SITE_METER,
    Kpis,
    compute_kpis,
    find_sql,
    load_carbon,
    load_tariff,
)

# Reused rather than re-derived: the run-period/design-day filter must never quietly diverge
# from what experiments.kpis uses for the same headline numbers.
from experiments.kpis import _run_period_env_indices as run_period_env_indices
from simulation.prepare_idf import PEOPLE_VARIABLES, ZONE_VARIABLES

log = get_logger("experiments.report")

J_PER_KWH = 3_600_000.0

#: |PMV| beyond this, in an occupied interval, counts as a comfort violation. Matches the
#: threshold already used in ``mcp_server/providers.py``'s KPI provider - one comfort
#: definition across the codebase, not two that could quietly disagree.
COMFORT_PMV_THRESHOLD = 0.5


# --------------------------------------------------------------------------------------
# Raw SQL reading (full timestamps - what Kpis deliberately does not carry)
# --------------------------------------------------------------------------------------


def _interval_midpoint(
    year: int, month: int, day: int, hour: int, minute: int, interval_min: int
) -> datetime:
    """Same construction as ``experiments.kpis._midpoint_hour``, but the whole datetime.

    EnergyPlus timestamps the *end* of an interval with ``Hour`` 0-24 and ``Minute`` 1-60, so
    building the end via ``timedelta`` from midnight sidesteps ``hour == 24`` correctly; the
    midpoint (end minus half the interval) is what places a sub-hourly reading in the right
    calendar day even right at a day boundary.
    """
    base_year = year if year and year >= 1 else 2017
    day_start = datetime(base_year, month, day)
    end = day_start + timedelta(hours=hour, minutes=minute)
    return end - timedelta(minutes=interval_min / 2.0)


def read_meter_series(sql_path: Path | str, meter: str = SITE_METER) -> pd.DataFrame:
    """Per-interval readings of ``meter`` with full timestamps.

    Columns: sim_time, kwh, interval_min.

    Warmup rows and non-run-period environments (design days) are excluded, identically to
    :func:`experiments.kpis.compute_kpis`.
    """
    conn = sqlite3.connect(str(sql_path))
    conn.row_factory = sqlite3.Row
    try:
        env_indices = run_period_env_indices(conn)
        rows = conn.execute(
            """
            SELECT rd.Value AS value_j,
                   t.Year AS year, t.Month AS month, t.Day AS day,
                   t.Hour AS hour, t.Minute AS minute, t.Interval AS interval_min,
                   t.EnvironmentPeriodIndex AS env_idx, t.WarmupFlag AS warmup
            FROM ReportData rd
            JOIN ReportDataDictionary rdd
                 ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
            JOIN Time t ON rd.TimeIndex = t.TimeIndex
            WHERE rdd.Name = ? AND (t.WarmupFlag = 0 OR t.WarmupFlag IS NULL)
            """,
            (meter,),
        ).fetchall()
    finally:
        conn.close()

    records = []
    for r in rows:
        env = r["env_idx"]
        if env_indices is not None and env is not None and int(env) not in env_indices:
            continue
        if not r["month"] or not r["day"]:
            continue
        interval_min = int(r["interval_min"]) if r["interval_min"] else 60
        stamp = _interval_midpoint(
            int(r["year"] or 2017), int(r["month"]), int(r["day"]),
            int(r["hour"] or 0), int(r["minute"] or 0), interval_min,
        )
        records.append((stamp, float(r["value_j"]) / J_PER_KWH, interval_min))

    frame = pd.DataFrame(records, columns=["sim_time", "kwh", "interval_min"])
    return frame.sort_values("sim_time").reset_index(drop=True)


def read_zone_series(sql_path: Path | str, model: PreparedModel) -> pd.DataFrame:
    """Per-interval zone comfort readings: sim_time, zone, air_temp_c, pmv, occupancy.

    PMV is keyed by People-object name in the SQL (the same asymmetry ``agent/bus.py`` handles
    at runtime); this joins it back onto the zone via ``model.zones[i].people``. A zone with no
    People object simply has no PMV column populated - it is not dropped from the table.
    """
    variables = {*ZONE_VARIABLES, *PEOPLE_VARIABLES}
    placeholders = ",".join("?" for _ in variables)

    conn = sqlite3.connect(str(sql_path))
    conn.row_factory = sqlite3.Row
    try:
        env_indices = run_period_env_indices(conn)
        rows = conn.execute(
            f"""
            SELECT rdd.Name AS variable, rdd.KeyValue AS key, rd.Value AS value,
                   t.Year AS year, t.Month AS month, t.Day AS day,
                   t.Hour AS hour, t.Minute AS minute, t.Interval AS interval_min,
                   t.EnvironmentPeriodIndex AS env_idx, t.WarmupFlag AS warmup
            FROM ReportData rd
            JOIN ReportDataDictionary rdd
                 ON rd.ReportDataDictionaryIndex = rdd.ReportDataDictionaryIndex
            JOIN Time t ON rd.TimeIndex = t.TimeIndex
            WHERE rdd.Name IN ({placeholders})
              AND (t.WarmupFlag = 0 OR t.WarmupFlag IS NULL)
            """,
            tuple(variables),
        ).fetchall()
    finally:
        conn.close()

    samples: dict[datetime, dict[tuple[str, str], float]] = {}
    for r in rows:
        env = r["env_idx"]
        if env_indices is not None and env is not None and int(env) not in env_indices:
            continue
        if not r["month"] or not r["day"]:
            continue
        interval_min = int(r["interval_min"]) if r["interval_min"] else 60
        stamp = _interval_midpoint(
            int(r["year"] or 2017), int(r["month"]), int(r["day"]),
            int(r["hour"] or 0), int(r["minute"] or 0), interval_min,
        )
        samples.setdefault(stamp, {})[(r["variable"], str(r["key"]).upper())] = float(r["value"])

    zone_air_temp, zone_occ, zone_pmv = ZONE_VARIABLES[0], ZONE_VARIABLES[1], PEOPLE_VARIABLES[0]
    records = []
    for stamp in sorted(samples):
        channels = samples[stamp]
        for binding in model.zones:
            air_temp = channels.get((zone_air_temp, binding.zone.upper()))
            if air_temp is None:
                continue
            occupancy = channels.get((zone_occ, binding.zone.upper()))
            pmv = channels.get((zone_pmv, binding.people.upper())) if binding.people else None
            records.append((stamp, binding.zone, air_temp, pmv, occupancy))

    return pd.DataFrame(records, columns=["sim_time", "zone", "air_temp_c", "pmv", "occupancy"])


# --------------------------------------------------------------------------------------
# Derived tables
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyRow:
    date: str
    kwh: float
    cost_inr: float
    carbon_kg: float
    peak_kw: float


def daily_breakdown(
    meter_series: pd.DataFrame, tariff: dict[int, float], carbon: dict[int, float]
) -> list[DailyRow]:
    """One row per calendar day the run actually covered - never averaged or dropped."""
    if meter_series.empty:
        return []
    frame = meter_series.copy()
    frame["hour"] = frame["sim_time"].dt.hour
    frame["day"] = frame["sim_time"].dt.date
    frame["cost"] = frame["kwh"] * frame["hour"].map(tariff).fillna(0.0)
    frame["carbon_g"] = frame["kwh"] * frame["hour"].map(carbon).fillna(0.0)
    frame["kw"] = frame.apply(
        lambda r: r["kwh"] / (r["interval_min"] / 60.0) if r["interval_min"] else 0.0, axis=1
    )

    rows = []
    for day, group in frame.groupby("day", sort=True):
        rows.append(
            DailyRow(
                date=day.isoformat(),
                kwh=float(group["kwh"].sum()),
                cost_inr=float(group["cost"].sum()),
                carbon_kg=float(group["carbon_g"].sum()) / 1000.0,
                peak_kw=float(group["kw"].max()),
            )
        )
    return rows


def cumulative_series(meter_series: pd.DataFrame) -> list[tuple[str, float]]:
    """Running total kWh over time - the dashboard's race-chart data, exported as-is."""
    if meter_series.empty:
        return []
    frame = meter_series.sort_values("sim_time")
    cumulative = frame["kwh"].cumsum()
    stamps = frame["sim_time"].apply(lambda ts: ts.isoformat())
    return list(zip(stamps, cumulative.round(6), strict=True))


@dataclass(frozen=True)
class ComfortRow:
    zone: str
    occupied_intervals: int
    violation_intervals: int
    occupied_hours: float
    violation_hours: float
    pct_of_occupied_hours: float
    worst_excursion_pmv: float


def comfort_table(
    zone_series: pd.DataFrame, *, timestep_minutes: int, threshold: float = COMFORT_PMV_THRESHOLD
) -> list[ComfortRow]:
    """One row per zone, plus a combined ``ALL`` row. Every zone the run produced, always.

    A "violation" is an occupied interval (``occupancy > 0``) with ``|PMV| > threshold``.
    ``worst_excursion_pmv`` is how far past the threshold the worst reading got - 0 if the
    zone never had PMV data or never violated, not omitted.
    """
    if zone_series.empty:
        return []
    step_hours = timestep_minutes / 60.0
    rows: list[ComfortRow] = []
    all_occupied = 0
    all_violations = 0
    all_worst = 0.0

    for zone, group in zone_series.groupby("zone", sort=True):
        occupied = group[group["occupancy"].fillna(0) > 0]
        occ_count = len(occupied)
        with_pmv = occupied[occupied["pmv"].notna()]
        violations = with_pmv[with_pmv["pmv"].abs() > threshold]
        worst = float(with_pmv["pmv"].abs().max() - threshold) if not with_pmv.empty else 0.0
        worst = max(0.0, worst)

        rows.append(
            ComfortRow(
                zone=zone,
                occupied_intervals=occ_count,
                violation_intervals=len(violations),
                occupied_hours=occ_count * step_hours,
                violation_hours=len(violations) * step_hours,
                pct_of_occupied_hours=(100.0 * len(violations) / occ_count) if occ_count else 0.0,
                worst_excursion_pmv=worst,
            )
        )
        all_occupied += occ_count
        all_violations += len(violations)
        all_worst = max(all_worst, worst)

    rows.append(
        ComfortRow(
            zone="ALL",
            occupied_intervals=all_occupied,
            violation_intervals=all_violations,
            occupied_hours=all_occupied * step_hours,
            violation_hours=all_violations * step_hours,
            pct_of_occupied_hours=(100.0 * all_violations / all_occupied) if all_occupied else 0.0,
            worst_excursion_pmv=all_worst,
        )
    )
    return rows


# --------------------------------------------------------------------------------------
# Per-arm assembly
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmReport:
    label: str
    sql_path: str
    kpis: Kpis
    daily: list[DailyRow] = field(default_factory=list)
    cumulative_kwh: list[tuple[str, float]] = field(default_factory=list)
    comfort: list[ComfortRow] = field(default_factory=list)


def build_arm_report(
    label: str,
    arm_dir: Path,
    *,
    model: PreparedModel,
    tariff_path: Path,
    carbon_path: Path,
) -> ArmReport:
    """Read one arm's ``eplusout.sql`` and produce everything the report needs from it."""
    sql_path = find_sql(arm_dir)
    tariff = load_tariff(tariff_path)
    carbon = load_carbon(carbon_path)

    kpis = compute_kpis(sql_path, tariff_path, carbon_path, run_label=label)
    meter_series = read_meter_series(sql_path)
    zone_series = read_zone_series(sql_path, model)

    return ArmReport(
        label=label,
        sql_path=str(sql_path),
        kpis=kpis,
        daily=daily_breakdown(meter_series, tariff, carbon),
        cumulative_kwh=cumulative_series(meter_series),
        comfort=comfort_table(zone_series, timestep_minutes=kpis.timestep_minutes),
    )


# --------------------------------------------------------------------------------------
# Deltas between arms
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Delta:
    label: str  # e.g. "agent vs baseline"
    from_arm: str
    to_arm: str
    site_kwh_from: float
    site_kwh_to: float
    site_kwh_pct: float | None  # None when `from` is 0 - avoid fabricating a percentage
    hvac_kwh_from: float
    hvac_kwh_to: float
    hvac_kwh_pct: float | None
    hvac_breakdown_pct: dict[str, float | None]
    cost_saved_inr: float
    carbon_avoided_kg: float
    peak_kw_reduction: float


def _pct_change(before: float, after: float) -> float | None:
    """``(before - after) / before * 100`` - positive means ``after`` is lower (a saving).

    ``None``, not 0 or inf, when ``before`` is zero: there is no percentage to report, and
    pretending otherwise would be fabricating a number the run did not produce.
    """
    if before == 0:
        return None
    return 100.0 * (before - after) / before


def compute_delta(from_report: ArmReport, to_report: ArmReport) -> Delta:
    """``to_report`` measured against ``from_report`` (e.g. agent measured against baseline)."""
    a, b = from_report.kpis, to_report.kpis
    breakdown_pct = {
        meter: _pct_change(
            a.hvac_breakdown_kwh.get(meter, 0.0), b.hvac_breakdown_kwh.get(meter, 0.0)
        )
        for meter in HVAC_METERS
    }
    return Delta(
        label=f"{to_report.label} vs {from_report.label}",
        from_arm=from_report.label,
        to_arm=to_report.label,
        site_kwh_from=a.site_kwh,
        site_kwh_to=b.site_kwh,
        site_kwh_pct=_pct_change(a.site_kwh, b.site_kwh),
        hvac_kwh_from=a.hvac_kwh,
        hvac_kwh_to=b.hvac_kwh,
        hvac_kwh_pct=_pct_change(a.hvac_kwh, b.hvac_kwh),
        hvac_breakdown_pct=breakdown_pct,
        cost_saved_inr=a.cost_inr - b.cost_inr,
        carbon_avoided_kg=a.carbon_kg - b.carbon_kg,
        peak_kw_reduction=a.peak_demand_kw - b.peak_demand_kw,
    )


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    generated_at: str
    spec_label: str
    arms: dict[str, ArmReport]
    deltas: list[Delta]

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "spec_label": self.spec_label,
            "arms": {label: asdict(arm) for label, arm in self.arms.items()},
            "deltas": [asdict(d) for d in self.deltas],
        }

    def to_json(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        return out

    def to_markdown(self) -> str:
        return _render_markdown(self)

    def write_markdown(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_markdown(), encoding="utf-8")
        return out


def build_report(
    arm_dirs: dict[str, Path],
    *,
    model: PreparedModel,
    tariff_path: Path,
    carbon_path: Path,
    spec_label: str = "",
) -> Report:
    """``arm_dirs`` maps arm label (``baseline``/``constant``/``agent``) to its output dir."""
    arms = {
        label: build_arm_report(
            label, arm_dir, model=model, tariff_path=tariff_path, carbon_path=carbon_path
        )
        for label, arm_dir in arm_dirs.items()
    }

    deltas: list[Delta] = []
    if "baseline" in arms and "agent" in arms:
        deltas.append(compute_delta(arms["baseline"], arms["agent"]))
    if "constant" in arms:
        if "baseline" in arms:
            deltas.append(compute_delta(arms["baseline"], arms["constant"]))
        if "agent" in arms:
            deltas.append(compute_delta(arms["constant"], arms["agent"]))

    return Report(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        spec_label=spec_label,
        arms=arms,
        deltas=deltas,
    )


# --------------------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------------------


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "n/a"


def _render_markdown(report: Report) -> str:
    lines = [
        "# HIVE results",
        "",
        f"Generated {report.generated_at} · run period `{report.spec_label}`",
        "",
    ]

    headline = next(
        (d for d in report.deltas if d.from_arm == "baseline" and d.to_arm == "agent"), None
    )
    if headline is not None:
        lines += [
            "## Headline",
            "",
            f"- **Total site electricity: {_fmt_pct(headline.site_kwh_pct)}** "
            f"({headline.site_kwh_from:.1f} -> {headline.site_kwh_to:.1f} kWh)",
            f"- HVAC subsystem (cooling + fans + pumps electricity, labeled): "
            f"{_fmt_pct(headline.hvac_kwh_pct)} "
            f"({headline.hvac_kwh_from:.1f} -> {headline.hvac_kwh_to:.1f} kWh)",
            f"- Cost saved: {headline.cost_saved_inr:+.2f} INR",
            f"- Carbon avoided: {headline.carbon_avoided_kg:+.2f} kgCO2",
            f"- Peak demand reduction: {headline.peak_kw_reduction:+.2f} kW",
            "",
        ]

    if report.deltas:
        lines += [
            "## All comparisons",
            "",
            "| comparison | site kWh Δ% | HVAC kWh Δ% | ₹ saved | kgCO2 avoided | peak kW Δ |",
            "|---|---|---|---|---|---|",
        ]
        for d in report.deltas:
            lines.append(
                f"| {d.label} | {_fmt_pct(d.site_kwh_pct)} | {_fmt_pct(d.hvac_kwh_pct)} | "
                f"{d.cost_saved_inr:+.2f} | {d.carbon_avoided_kg:+.2f} | "
                f"{d.peak_kw_reduction:+.2f} |"
            )
        lines.append("")
        lines += [
            "### HVAC subsystem breakdown by meter",
            "",
            "| comparison | " + " | ".join(HVAC_METERS) + " |",
            "|---|" + "---|" * len(HVAC_METERS),
        ]
        for d in report.deltas:
            cells = " | ".join(_fmt_pct(d.hvac_breakdown_pct.get(m)) for m in HVAC_METERS)
            lines.append(f"| {d.label} | {cells} |")
        lines.append("")

    lines += [
        "## Per-arm KPIs",
        "",
        "| arm | site kWh | HVAC kWh | peak kW | cost INR | carbon kg |",
        "|---|---|---|---|---|---|",
    ]
    for label, arm in report.arms.items():
        k = arm.kpis
        lines.append(
            f"| {label} | {k.site_kwh:.1f} | {k.hvac_kwh:.1f} | {k.peak_demand_kw:.2f} | "
            f"{k.cost_inr:.2f} | {k.carbon_kg:.2f} |"
        )
    lines.append("")

    lines += ["## Comfort violations (occupied hours, |PMV| > 0.5)", ""]
    for label, arm in report.arms.items():
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| zone | occupied h | violation h | % of occupied | worst excursion (PMV) |")
        lines.append("|---|---|---|---|---|")
        for row in arm.comfort:
            lines.append(
                f"| {row.zone} | {row.occupied_hours:.2f} | {row.violation_hours:.2f} | "
                f"{row.pct_of_occupied_hours:.1f}% | {row.worst_excursion_pmv:.3f} |"
            )
        lines.append("")

    lines += ["## Per-day breakdown", ""]
    for label, arm in report.arms.items():
        lines.append(f"### {label}")
        lines.append("")
        lines.append("| date | kWh | cost INR | carbon kg | peak kW |")
        lines.append("|---|---|---|---|---|")
        for row in arm.daily:
            lines.append(
                f"| {row.date} | {row.kwh:.2f} | {row.cost_inr:.2f} | {row.carbon_kg:.3f} | "
                f"{row.peak_kw:.2f} |"
            )
        lines.append("")

    lines += [
        "## Cumulative kWh series",
        "",
        "Full per-arm, per-timestep cumulative series is in `results.json` "
        "(`arms.<label>.cumulative_kwh`) for the dashboard's race chart; omitted here for length.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def _default_ab_dir(settings: Settings) -> Path:
    root = settings.repo_root / "experiments" / "results"
    candidates = sorted(root.glob("ab_*"), reverse=True) if root.is_dir() else []
    if not candidates:
        raise FileNotFoundError(
            f"no A/B run found under {root}. Run `python -m experiments.ab` first, "
            "or pass --ab-dir explicitly."
        )
    return candidates[0]


def _arm_dirs(ab_dir: Path) -> dict[str, Path]:
    from experiments.ab import AGENT_LABEL, BASELINE_LABEL, CONSTANT_LABEL

    arms = {}
    for label in (BASELINE_LABEL, CONSTANT_LABEL, AGENT_LABEL):
        candidate = ab_dir / label
        if candidate.is_dir():
            arms[label] = candidate
    if not arms:
        raise FileNotFoundError(f"no arm subdirectories found under {ab_dir}")
    return arms


def main(argv: list[str] | None = None) -> int:
    import argparse

    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Build results.json / results.md from an A/B run.")
    parser.add_argument("--ab-dir", type=Path, default=None, help="an experiments/results/ab_* dir")
    parser.add_argument("--tariff", type=Path, default=settings.data_dir / "tariff.csv")
    parser.add_argument("--carbon", type=Path, default=settings.data_dir / "carbon_intensity.csv")
    parser.add_argument(
        "--json-out", type=Path, default=settings.repo_root / "reports" / "results.json"
    )
    parser.add_argument(
        "--md-out", type=Path, default=settings.repo_root / "reports" / "results.md"
    )
    args = parser.parse_args(argv)

    ab_dir = args.ab_dir or _default_ab_dir(settings)
    arm_dirs = _arm_dirs(ab_dir)

    index_path = settings.simulation_dir / "agentic_model.json"
    if not index_path.is_file():
        raise FileNotFoundError(
            f"{index_path} not found. Run `python -m simulation.prepare_idf` first."
        )
    model = PreparedModel.load(index_path)

    report = build_report(
        arm_dirs, model=model, tariff_path=args.tariff, carbon_path=args.carbon,
        spec_label=ab_dir.name,
    )
    report.to_json(args.json_out)
    report.write_markdown(args.md_out)

    print(f"wrote {args.json_out}")
    print(f"wrote {args.md_out}")
    headline = next(
        (d for d in report.deltas if d.from_arm == "baseline" and d.to_arm == "agent"), None
    )
    if headline is not None:
        print(f"headline: total site kWh {_fmt_pct(headline.site_kwh_pct)}")
    return 0


__all__ = [
    "COMFORT_PMV_THRESHOLD",
    "ArmReport",
    "ComfortRow",
    "DailyRow",
    "Delta",
    "Report",
    "build_arm_report",
    "build_report",
    "comfort_table",
    "compute_delta",
    "cumulative_series",
    "daily_breakdown",
    "read_meter_series",
    "read_zone_series",
]


if __name__ == "__main__":
    raise SystemExit(main())
