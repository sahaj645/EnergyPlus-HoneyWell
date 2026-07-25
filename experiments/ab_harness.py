"""A/B harness: baseline arm vs agent arm, everything else held constant.

Same IDF, same EPW, same simulation period, same run seed. The only difference between arms
is whether the guardian receives planner output or the fallback schedule.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from pathlib import Path

from common.models import KpiSnapshot


def run_arm(label: str, *, use_agent: bool, output_dir: Path) -> KpiSnapshot:
    """Run a single arm and return its KPI snapshot."""
    raise NotImplementedError("A/B arm runner not implemented yet (scaffold)")


def run_ab(output_dir: Path) -> dict[str, KpiSnapshot]:
    """Run both arms and return ``{"baseline": ..., "agent": ...}``."""
    raise NotImplementedError("A/B harness not implemented yet (scaffold)")


def compare(results: dict[str, KpiSnapshot]) -> str:
    """Render a human-readable delta table for ``reports/``."""
    raise NotImplementedError("A/B comparison not implemented yet (scaffold)")


__all__ = ["compare", "run_ab", "run_arm"]
