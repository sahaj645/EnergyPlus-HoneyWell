"""KPI extraction tests against a hand-built EnergyPlus-shaped SQLite fixture.

The fixture reproduces the parts of the E+ SQL schema that ``experiments.kpis`` reads
(``ReportDataDictionary``, ``ReportData``, ``Time``, ``EnvironmentPeriods``) with values whose
KPIs can be computed by hand, then asserts the join. It deliberately includes two rows that
must be *excluded* - one warmup row and one design-day-environment row, both with huge values -
so the test fails loudly if either filter regresses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from experiments import kpis

# Meter -> ReportDataDictionaryIndex in the fixture.
_DICT = {
    "Electricity:Facility": 1,
    "Cooling:Electricity": 2,
    "Fans:Electricity": 3,
    "Pumps:Electricity": 4,
}

J = 3_600_000.0  # 1 kWh in joules


def _build_fixture(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE ReportDataDictionary (
            ReportDataDictionaryIndex INTEGER PRIMARY KEY,
            IsMeter INTEGER, Type TEXT, IndexGroup TEXT, TimestepType TEXT,
            KeyValue TEXT, Name TEXT, ReportingFrequency TEXT, ScheduleName TEXT, Units TEXT
        );
        CREATE TABLE Time (
            TimeIndex INTEGER PRIMARY KEY,
            Year INTEGER, Month INTEGER, Day INTEGER, Hour INTEGER, Minute INTEGER,
            Dst INTEGER, Interval INTEGER, IntervalType INTEGER, SimulationDays INTEGER,
            DayType TEXT, EnvironmentPeriodIndex INTEGER, WarmupFlag INTEGER
        );
        CREATE TABLE ReportData (
            ReportDataIndex INTEGER PRIMARY KEY,
            TimeIndex INTEGER, ReportDataDictionaryIndex INTEGER, Value REAL
        );
        CREATE TABLE EnvironmentPeriods (
            EnvironmentPeriodIndex INTEGER PRIMARY KEY,
            SimulationIndex INTEGER, EnvironmentName TEXT, EnvironmentType INTEGER
        );
        """
    )

    for name, idx in _DICT.items():
        conn.execute(
            "INSERT INTO ReportDataDictionary "
            "(ReportDataDictionaryIndex, IsMeter, Name, ReportingFrequency, Units) "
            "VALUES (?, 1, ?, 'Timestep', 'J')",
            (idx, name),
        )

    # env 1 = weather run period (counts); env 2 = design day (must be excluded)
    conn.executemany(
        "INSERT INTO EnvironmentPeriods "
        "(EnvironmentPeriodIndex, SimulationIndex, EnvironmentName, EnvironmentType) "
        "VALUES (?,?,?,?)",
        [(1, 1, "HOTWEEK", 3), (2, 1, "DESIGNDAY", 1)],
    )

    # TimeIndex, (Y,M,D,H,Min), Interval, EnvIdx, Warmup
    times = [
        (1, 2017, 7, 1, 1, 0, 60, 1, 0),   # covers 00:00-01:00 -> hour 0   (counts)
        (2, 2017, 7, 1, 14, 0, 60, 1, 0),  # covers 13:00-14:00 -> hour 13  (counts)
        (3, 2017, 7, 1, 20, 0, 60, 1, 0),  # covers 19:00-20:00 -> hour 19  (counts)
        (4, 2017, 7, 1, 3, 0, 60, 1, 1),   # WARMUP -> excluded
        (5, 2017, 7, 1, 4, 0, 60, 2, 0),   # design-day env -> excluded
    ]
    for ti, y, m, d, h, mi, interval, env, warm in times:
        conn.execute(
            "INSERT INTO Time "
            "(TimeIndex, Year, Month, Day, Hour, Minute, Interval, "
            "EnvironmentPeriodIndex, WarmupFlag) VALUES (?,?,?,?,?,?,?,?,?)",
            (ti, y, m, d, h, mi, interval, env, warm),
        )

    # Facility electricity (kWh): T1=1.0, T2=2.0, T3=0.5 ; excluded rows are huge.
    facility = {1: 1.0 * J, 2: 2.0 * J, 3: 0.5 * J, 4: 1000.0 * J, 5: 1000.0 * J}
    cooling = {1: 0.5 * J, 2: 1.0 * J, 3: 0.5 * J}
    fans = {1: 0.1 * J, 2: 0.2 * J, 3: 0.1 * J}
    pumps = {2: 0.05 * J}

    rd_index = 1
    for time_idx, value in facility.items():
        conn.execute(
            "INSERT INTO ReportData VALUES (?,?,?,?)",
            (rd_index, time_idx, _DICT["Electricity:Facility"], value),
        )
        rd_index += 1
    for meter, series in (
        ("Cooling:Electricity", cooling),
        ("Fans:Electricity", fans),
        ("Pumps:Electricity", pumps),
    ):
        for time_idx, value in series.items():
            conn.execute(
                "INSERT INTO ReportData VALUES (?,?,?,?)", (rd_index, time_idx, _DICT[meter], value)
            )
            rd_index += 1

    conn.commit()
    conn.close()


@pytest.fixture
def eplus_sql(tmp_path: Path) -> Path:
    path = tmp_path / "eplusout.sql"
    _build_fixture(path)
    return path


@pytest.fixture
def data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def test_join_produces_hand_computed_cost_and_carbon(eplus_sql: Path, data_dir: Path) -> None:
    k = kpis.compute_kpis(
        eplus_sql,
        data_dir / "tariff.csv",
        data_dir / "carbon_intensity.csv",
        run_label="fixture",
    )

    # Site energy: 1.0 + 2.0 + 0.5 kWh (warmup + design-day rows excluded).
    assert k.site_kwh == pytest.approx(3.5)
    assert k.intervals_counted == 3

    # Peak: hourly intervals, so kW == kWh; max is the 2.0 kWh interval.
    assert k.peak_demand_kw == pytest.approx(2.0)

    # Cost = 1.0*tariff[0] + 2.0*tariff[13] + 0.5*tariff[19]
    #      = 1.0*6.10 + 2.0*6.50 + 0.5*11.90 = 25.05 INR
    assert k.cost_inr == pytest.approx(25.05)

    # Carbon = (1.0*712 + 2.0*481 + 0.5*845) g = 2096.5 g = 2.0965 kg
    assert k.carbon_kg == pytest.approx(2.0965)


def test_hvac_itemisation(eplus_sql: Path, data_dir: Path) -> None:
    k = kpis.compute_kpis(eplus_sql, data_dir / "tariff.csv", data_dir / "carbon_intensity.csv")
    assert k.hvac_breakdown_kwh["cooling"] == pytest.approx(2.0)
    assert k.hvac_breakdown_kwh["fans"] == pytest.approx(0.4)
    assert k.hvac_breakdown_kwh["pumps"] == pytest.approx(0.05)
    assert k.hvac_kwh == pytest.approx(2.45)


def test_timestep_is_inferred(eplus_sql: Path, data_dir: Path) -> None:
    k = kpis.compute_kpis(eplus_sql, data_dir / "tariff.csv", data_dir / "carbon_intensity.csv")
    assert k.timestep_minutes == 60  # from Time.Interval, not hardcoded


def test_warmup_and_design_day_are_excluded(eplus_sql: Path, data_dir: Path) -> None:
    # Both excluded rows carry 1000 kWh; if either filter regressed, site_kwh would explode.
    k = kpis.compute_kpis(eplus_sql, data_dir / "tariff.csv", data_dir / "carbon_intensity.csv")
    assert k.site_kwh < 10.0


def test_json_roundtrip(eplus_sql: Path, data_dir: Path, tmp_path: Path) -> None:
    import json

    k = kpis.compute_kpis(eplus_sql, data_dir / "tariff.csv", data_dir / "carbon_intensity.csv")
    out = tmp_path / "kpis.json"
    k.to_json(out)
    loaded = json.loads(out.read_text())
    assert loaded["site_kwh"] == pytest.approx(3.5)
    assert loaded["hvac_breakdown_kwh"]["cooling"] == pytest.approx(2.0)


def test_format_table_mentions_the_headline_numbers(eplus_sql: Path, data_dir: Path) -> None:
    k = kpis.compute_kpis(eplus_sql, data_dir / "tariff.csv", data_dir / "carbon_intensity.csv")
    table = kpis.format_table(k)
    assert "cost (ToU)" in table
    assert "kgCO2" in table
