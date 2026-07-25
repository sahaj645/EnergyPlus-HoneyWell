"""Run the baseline (control-arm) simulation: no agent, no actuation.

This is the A/B control arm *and* the fallback behaviour, which is deliberate - "agent
disabled" and "agent failed" must produce identical building behaviour. Every savings claim
this project makes is measured against the run this script produces.

What it does:

1. Picks a RunPeriod - by default the **hottest week** in the weather file (the interesting
   case for a cooling-dominated Indian climate), or a whole month (``--month``) or the whole
   year (``--annual``) for endurance work.
2. Patches ``baseline.idf`` via eppy: sets that RunPeriod, ensures ``Output:SQLite`` and the
   meters the KPI extractor needs, and sets the sub-hourly timestep.
3. Runs EnergyPlus through the **runtime API** (``pyenergyplus.api.EnergyPlusAPI``), not a
   subprocess, writing into ``simulation/out_baseline/``.
4. Reports the Severe/Fatal error count from ``eplusout.err``.

Heavy dependencies (``eppy``, ``pyenergyplus``) are imported lazily inside the functions that
need them, so importing this module costs nothing and works on a machine with no EnergyPlus -
the same discipline the Ollama and MCP clients follow. ``hottest_week`` is pure Python and is
unit-tested without an EnergyPlus install.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger

log = get_logger("simulation.baseline")

# Meters the KPI extractor (experiments/kpis.py) needs, requested at Timestep frequency so
# peak demand has sub-hourly resolution. Keep this list in step with experiments.kpis.
REQUIRED_METERS = (
    "Electricity:Facility",
    "Cooling:Electricity",
    "Fans:Electricity",
    "Pumps:Electricity",
    "Heating:Electricity",
    "InteriorLights:Electricity",
)

_EPW_HEADER_LINES = 8
_EPW_DRYBULB_COL = 6
_HOURS_PER_WEEK = 168
_SUMMER_HOURS = 8760

_SEVERE_RE = re.compile(r"\*\*\s*Severe", re.IGNORECASE)
_FATAL_RE = re.compile(r"\*\*\s*Fatal", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"(\d+)\s+Warning;\s*(\d+)\s+Severe", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# RunPeriod selection
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunPeriodSpec:
    """A simulation window, as month/day pairs (year-agnostic, like an E+ RunPeriod)."""

    label: str
    begin_month: int
    begin_day: int
    end_month: int
    end_day: int


def _read_epw_drybulb(epw_path: Path | str) -> list[float]:
    """Return the dry-bulb temperature series (deg C) from an EPW, in chronological order."""
    temps: list[float] = []
    with open(epw_path, encoding="utf-8", errors="replace") as fh:
        for _ in range(_EPW_HEADER_LINES):
            next(fh, None)
        for line in fh:
            fields = line.split(",")
            if len(fields) <= _EPW_DRYBULB_COL:
                continue
            try:
                temps.append(float(fields[_EPW_DRYBULB_COL]))
            except ValueError:
                continue
    return temps


def hottest_week(epw_path: Path | str) -> RunPeriodSpec:
    """Find the 7-day window with the highest mean dry-bulb temperature.

    Uses the hour index rather than the EPW's per-row year (TMY files stitch multiple years
    together), mapping onto a nominal non-leap calendar so the result is a clean month/day
    pair for the RunPeriod.
    """
    temps = _read_epw_drybulb(epw_path)
    if len(temps) < _HOURS_PER_WEEK:
        raise ValueError(
            f"{epw_path} has only {len(temps)} hourly records; need >= {_HOURS_PER_WEEK}"
        )

    window = _HOURS_PER_WEEK
    running = sum(temps[:window])
    best_sum, best_start = running, 0
    for i in range(1, len(temps) - window + 1):
        running += temps[i + window - 1] - temps[i - 1]
        if running > best_sum:
            best_sum, best_start = running, i

    start_doy = best_start // 24
    begin = date(2017, 1, 1) + timedelta(days=start_doy)
    end = begin + timedelta(days=6)
    return RunPeriodSpec(
        label=f"hottest_week_{begin:%m%d}",
        begin_month=begin.month,
        begin_day=begin.day,
        end_month=end.month,
        end_day=end.day,
    )


def month_spec(month: int) -> RunPeriodSpec:
    """A whole-month RunPeriod."""
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1-12, got {month}")
    end = (date(2017, month + 1, 1) if month < 12 else date(2018, 1, 1)) - timedelta(days=1)
    return RunPeriodSpec(f"month_{month:02d}", month, 1, end.month, end.day)


ANNUAL_SPEC = RunPeriodSpec("annual", 1, 1, 12, 31)


# --------------------------------------------------------------------------------------
# IDF patching (eppy)
# --------------------------------------------------------------------------------------


def _load_idf(idf_path: Path, install_dir: Path):
    """Open an IDF with eppy, pointing it at the installed Energy+.idd."""
    from eppy.modeleditor import IDF

    idd = install_dir / "Energy+.idd"
    if not idd.is_file():
        raise FileNotFoundError(
            f"Energy+.idd not found at {idd}; is {install_dir} an EnergyPlus root?"
        )
    IDF.setiddname(str(idd))
    return IDF(str(idf_path))


def patch_idf(idf, spec: RunPeriodSpec, *, timesteps_per_hour: int = 6):
    """Set the RunPeriod, timestep, SQLite output and required meters on ``idf`` in place."""
    _set_run_period(idf, spec)
    _set_timestep(idf, timesteps_per_hour)
    _ensure_sqlite(idf)
    _ensure_meters(idf, REQUIRED_METERS)
    return idf


def _set_run_period(idf, spec: RunPeriodSpec) -> None:
    run_periods = idf.idfobjects.get("RUNPERIOD", [])
    rp = run_periods[0] if run_periods else idf.newidfobject("RUNPERIOD")
    rp.Name = spec.label
    rp.Begin_Month = spec.begin_month
    rp.Begin_Day_of_Month = spec.begin_day
    rp.End_Month = spec.end_month
    rp.End_Day_of_Month = spec.end_day
    # Weather-driven, not design-day. Field names vary a little across versions; set defensively.
    for fieldname, value in (
        ("Use_Weather_File_Holidays_and_Special_Days", "Yes"),
        ("Use_Weather_File_Daylight_Saving_Period", "Yes"),
        ("Apply_Weekend_Holiday_Rule", "No"),
        ("Use_Weather_File_Rain_Indicators", "Yes"),
        ("Use_Weather_File_Snow_Indicators", "Yes"),
    ):
        if fieldname in rp.fieldnames:
            setattr(rp, fieldname, value)


def _set_timestep(idf, timesteps_per_hour: int) -> None:
    steps = idf.idfobjects.get("TIMESTEP", [])
    ts = steps[0] if steps else idf.newidfobject("TIMESTEP")
    ts.Number_of_Timesteps_per_Hour = timesteps_per_hour


def _ensure_sqlite(idf) -> None:
    existing = idf.idfobjects.get("OUTPUT:SQLITE", [])
    if existing:
        existing[0].Option_Type = "SimpleAndTabular"
    else:
        idf.newidfobject("OUTPUT:SQLITE", Option_Type="SimpleAndTabular")


def _ensure_meters(idf, meters: tuple[str, ...]) -> None:
    present = {m.Key_Name for m in idf.idfobjects.get("OUTPUT:METER", [])}
    for meter in meters:
        if meter not in present:
            idf.newidfobject("OUTPUT:METER", Key_Name=meter, Reporting_Frequency="Timestep")


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------


def _run_energyplus(idf_file: Path, epw_file: Path, out_dir: Path) -> int:
    """Invoke EnergyPlus through the runtime API. Returns the process exit code."""
    from pyenergyplus.api import EnergyPlusAPI

    api = EnergyPlusAPI()
    state = api.state_manager.new_state()
    try:
        return api.runtime.run_energyplus(
            state,
            ["-w", str(epw_file), "-d", str(out_dir), str(idf_file)],
        )
    finally:
        api.state_manager.delete_state(state)


def count_errors(err_path: Path) -> tuple[int, int]:
    """Return ``(severe, fatal)`` counts from an ``eplusout.err`` file.

    Prefers the summary line EnergyPlus writes at the end; falls back to counting markers.
    """
    if not err_path.is_file():
        return (0, 0)
    text = err_path.read_text(encoding="utf-8", errors="replace")

    summary = _SUMMARY_RE.search(text)
    severe = int(summary.group(2)) if summary else len(_SEVERE_RE.findall(text))
    fatal = len(_FATAL_RE.findall(text))
    return (severe, fatal)


def run(
    settings: Settings | None = None,
    *,
    spec: RunPeriodSpec | None = None,
    timesteps_per_hour: int = 6,
    out_dir: Path | None = None,
) -> Path:
    """Run the baseline simulation and return the output directory.

    Requires a real EnergyPlus install (``ENERGYPLUS_DIR``). Raises if any Fatal error occurs;
    Severe errors are reported but do not raise - the caller decides.
    """
    settings = settings or Settings.from_env()
    install_dir = eplus_path.require_energyplus()

    if not settings.idf_path.is_file():
        raise FileNotFoundError(
            f"{settings.idf_path} not found. Run `python simulation/fetch_assets.py` first."
        )
    if not settings.epw_path.is_file():
        raise FileNotFoundError(
            f"{settings.epw_path} not found. Run `python simulation/fetch_assets.py` first."
        )

    spec = spec or hottest_week(settings.epw_path)
    out_dir = out_dir or (settings.simulation_dir / "out_baseline")
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("run period: %s (%02d/%02d -> %02d/%02d)", spec.label,
             spec.begin_month, spec.begin_day, spec.end_month, spec.end_day)

    idf = _load_idf(settings.idf_path, install_dir)
    patch_idf(idf, spec, timesteps_per_hour=timesteps_per_hour)
    patched = out_dir / "baseline_patched.idf"
    idf.saveas(str(patched))
    log.info("patched IDF written to %s", patched)

    exit_code = _run_energyplus(patched, settings.epw_path, out_dir)

    severe, fatal = count_errors(out_dir / "eplusout.err")
    log.info("EnergyPlus exit=%s severe=%d fatal=%d", exit_code, severe, fatal)
    if fatal or exit_code != 0:
        raise RuntimeError(
            f"EnergyPlus did not complete cleanly: exit={exit_code}, fatal={fatal}. "
            f"See {out_dir / 'eplusout.err'}."
        )
    if severe:
        log.warning(
            "%d Severe error(s) - inspect %s before trusting KPIs", severe, out_dir / "eplusout.err"
        )
    return out_dir


def _resolve_spec(args: argparse.Namespace, settings: Settings) -> RunPeriodSpec:
    if args.annual:
        return ANNUAL_SPEC
    if args.month is not None:
        return month_spec(args.month)
    return hottest_week(settings.epw_path)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python simulation/run_baseline.py``."""
    parser = argparse.ArgumentParser(description="Run the HIVE baseline (control-arm) simulation.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--annual", action="store_true", help="simulate the full year (endurance)")
    group.add_argument("--month", type=int, metavar="N", help="simulate whole month N (1-12)")
    parser.add_argument("--timestep", type=int, default=6, dest="timesteps_per_hour",
                        help="timesteps per hour (default 6 = 10 min)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    # require_energyplus() first: a clear message beats an EPW-parse error when E+ is missing.
    eplus_path.require_energyplus()
    spec = _resolve_spec(args, settings)

    out_dir = run(settings, spec=spec, timesteps_per_hour=args.timesteps_per_hour)
    severe, fatal = count_errors(out_dir / "eplusout.err")
    print(f"baseline run complete: {out_dir}  (severe={severe}, fatal={fatal})")
    print(f"next: python -m experiments.kpis {out_dir}")
    return 1 if (severe or fatal) else 0


if __name__ == "__main__":
    raise SystemExit(main())
