"""Turn raw telemetry into the numbers that go in the report.

Reads the SQLite database read-only and joins facility power against ``data/tariff.csv`` and
``data/carbon_intensity.csv`` to produce cost and carbon. Comfort violations are counted in
occupied hours only - penalising an empty building for drifting is how you get a control
system that optimises the wrong thing.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from common.models import KpiSnapshot


def load_telemetry(db_path: Path) -> pd.DataFrame:
    """Read the ``telemetry`` table into a tidy frame."""
    raise NotImplementedError("telemetry loading not implemented yet (scaffold)")


def load_price_and_carbon(data_dir: Path) -> pd.DataFrame:
    """Load the representative ToU tariff and grid carbon-intensity curves."""
    raise NotImplementedError("tariff/carbon loading not implemented yet (scaffold)")


def compute_kpis(db_path: Path, data_dir: Path) -> KpiSnapshot:
    """Energy, cost, carbon, peak demand and comfort violations for a whole run."""
    raise NotImplementedError("KPI computation not implemented yet (scaffold)")


def export_csv(db_path: Path, out_path: Path) -> Path:
    """Dump the joined per-hour frame to ``reports/`` for the write-up."""
    raise NotImplementedError("KPI export not implemented yet (scaffold)")


__all__ = ["compute_kpis", "export_csv", "load_price_and_carbon", "load_telemetry"]
