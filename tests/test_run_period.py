"""Tests for RunPeriod selection in ``simulation.run_baseline``.

These exercise the pure-Python logic (EPW parsing, hottest-week search, month spec) that ships
in the baseline runner, so the parts that do *not* need an EnergyPlus install are still
verified. The EnergyPlus run itself requires a real install and is out of scope here.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from simulation import run_baseline as rb


def _write_epw(path: Path, drybulb_by_hour: list[float]) -> None:
    """Write a minimal but valid-enough EPW: 8 header lines then 8760 data rows."""
    header = [
        "LOCATION,Test,,,,,,,0.0,0.0,0.0,0.0",
        "DESIGN CONDITIONS,0",
        "TYPICAL/EXTREME PERIODS,0",
        "GROUND TEMPERATURES,0",
        "HOLIDAYS/DAYLIGHT SAVINGS,No,0,0,0",
        "COMMENTS 1,synthetic",
        "COMMENTS 2,synthetic",
        "DATA PERIODS,1,1,Data,Sunday,1/1,12/31",
    ]
    lines = list(header)
    start = date(2017, 1, 1)
    for i, temp in enumerate(drybulb_by_hour):
        day = start + timedelta(days=i // 24)
        hour = i % 24 + 1  # EPW hour is 1-24
        # cols: year,month,day,hour,minute,flags,drybulb,...
        lines.append(f"2017,{day.month},{day.day},{hour},60,_,{temp:.1f},0,0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_hottest_week_finds_the_spike(tmp_path: Path) -> None:
    # Baseline 20 C everywhere, a hot plateau of 40 C for the 7 days starting at day-of-year 200.
    temps = [20.0] * rb._SUMMER_HOURS
    spike_start = 200 * 24
    for h in range(spike_start, spike_start + rb._HOURS_PER_WEEK):
        temps[h] = 40.0
    epw = tmp_path / "weather.epw"
    _write_epw(epw, temps)

    spec = rb.hottest_week(epw)

    expected = date(2017, 1, 1) + timedelta(days=200)
    assert (spec.begin_month, spec.begin_day) == (expected.month, expected.day)
    end = expected + timedelta(days=6)
    assert (spec.end_month, spec.end_day) == (end.month, end.day)
    assert spec.label.startswith("hottest_week_")


def test_hottest_week_rejects_short_files(tmp_path: Path) -> None:
    epw = tmp_path / "short.epw"
    # 8 header lines then only 10 hourly data rows — far fewer than a week.
    data_rows = [f"2017,1,1,{h + 1},60,_,25.0,0,0" for h in range(10)]
    lines = ["LOCATION", *([""] * 7), *data_rows]
    epw.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="need >="):
        rb.hottest_week(epw)


def test_month_spec_boundaries() -> None:
    feb = rb.month_spec(2)
    assert (feb.begin_month, feb.begin_day) == (2, 1)
    assert (feb.end_month, feb.end_day) == (2, 28)  # 2017 is not a leap year

    dec = rb.month_spec(12)
    assert (dec.end_month, dec.end_day) == (12, 31)


def test_month_spec_validates_range() -> None:
    with pytest.raises(ValueError):
        rb.month_spec(13)


def test_count_errors_parses_summary(tmp_path: Path) -> None:
    err = tmp_path / "eplusout.err"
    err.write_text(
        "Beginning...\n"
        "   ** Warning ** something benign\n"
        " *** EnergyPlus Completed Successfully-- 3 Warning; 0 Severe Errors\n",
        encoding="utf-8",
    )
    assert rb.count_errors(err) == (0, 0)


def test_count_errors_counts_markers_without_summary(tmp_path: Path) -> None:
    err = tmp_path / "eplusout.err"
    err.write_text(
        "   ** Severe  ** bad node\n"
        "   ** Severe  ** another one\n"
        "   **  Fatal  ** giving up\n",
        encoding="utf-8",
    )
    assert rb.count_errors(err) == (2, 1)


def test_annual_spec_is_full_year() -> None:
    assert (rb.ANNUAL_SPEC.begin_month, rb.ANNUAL_SPEC.begin_day) == (1, 1)
    assert (rb.ANNUAL_SPEC.end_month, rb.ANNUAL_SPEC.end_day) == (12, 31)
