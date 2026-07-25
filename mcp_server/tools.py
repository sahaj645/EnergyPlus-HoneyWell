"""The six tools. Signatures are the contract; implementations come later.

All payloads in and out are :mod:`common.models` types - the tool layer does no schema
translation of its own (CLAUDE.md, rule R4).

Scaffold only: no logic yet.
"""

from __future__ import annotations

from datetime import datetime

from common.models import ApprovedPlan, BuildingState, ForecastPoint, KpiSnapshot, Plan

# --------------------------------------------------------------------------------------
# Read-only observation
# --------------------------------------------------------------------------------------


def get_state() -> BuildingState:
    """Current zone conditions, outdoor weather and facility power."""
    raise NotImplementedError("get_state not implemented yet (scaffold)")


def get_forecasts(hours: int = 12) -> list[ForecastPoint]:
    """Look-ahead weather, tariff (``data/tariff.csv``) and grid carbon."""
    raise NotImplementedError("get_forecasts not implemented yet (scaffold)")


def get_kpis(since: datetime | None = None) -> KpiSnapshot:
    """Energy, cost, carbon, peak demand and comfort violations for the run so far."""
    raise NotImplementedError("get_kpis not implemented yet (scaffold)")


def read_error_log(lines: int = 50) -> str:
    """Tail of the EnergyPlus ``.err`` file.

    This is what lets the agent notice it has broken the model - severe warnings and fatal
    errors are how a bad ``patch_model`` announces itself.
    """
    raise NotImplementedError("read_error_log not implemented yet (scaffold)")


# --------------------------------------------------------------------------------------
# Mutating
# --------------------------------------------------------------------------------------


def submit_plan(plan: Plan) -> ApprovedPlan:
    """Hand a plan to the guardian and return its verdict.

    **Does not actuate.** The returned :class:`~common.models.ApprovedPlan` is deposited in
    the plan cache; the EnergyPlus callback picks it up on its own schedule.
    """
    raise NotImplementedError("submit_plan not implemented yet (scaffold)")


def patch_model(edits: dict[str, object], *, note: str = "") -> str:
    """Apply an IDF edit via eppy, writing a new ``simulation/vN_*.idf``.

    Never mutates ``baseline.idf``: every change is a new numbered version, so any run can be
    traced back to the exact model that produced it. Returns the new version's path.
    """
    raise NotImplementedError("patch_model not implemented yet (scaffold)")


TOOL_NAMES = (
    "get_state",
    "get_forecasts",
    "get_kpis",
    "submit_plan",
    "read_error_log",
    "patch_model",
)

__all__ = [
    "TOOL_NAMES",
    "get_forecasts",
    "get_kpis",
    "get_state",
    "patch_model",
    "read_error_log",
    "submit_plan",
]
