"""The six tools, as pure functions over a :class:`ToolContext`.

Nothing here imports the ``mcp`` SDK - :mod:`mcp_server.server` wraps these into an MCP server.
Keeping them SDK-free means they are directly callable (the exercise script does exactly that)
and the whole surface stays testable without a transport.

All payloads in and out are :mod:`common.models` types (rule R4); the tool layer does no schema
translation of its own. The docstrings on the *server* wrappers are the LLM-facing contract;
these implementations carry the mechanics.

**Rule R2 lives in :func:`submit_plan`.** It validates the plan, lowers it, and runs it through
the very same ``guardian.core.Guardian`` the executor uses - returning the guardian's verdict,
never actuating. There is no argument, flag, or code path here that reaches an actuator without
the guardian. ``patch_model`` is the one tool that can change the *model* rather than a plan;
its blast radius is real and is discussed in CLAUDE.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from common.log import get_logger
from common.models import (
    ApprovedPlan,
    BuildingState,
    ForecastPoint,
    GuardianDecision,
    KpiSnapshot,
    PatchSpec,
    Plan,
    PreparedModel,
)
from common.planslot import PlanSlot
from guardian.core import Guardian, RateHistory

log = get_logger("mcp_server.tools")

_ERR_SEVERE_MARKERS = ("** severe", "**  fatal")


class ToolError(RuntimeError):
    """A tool could not complete. Surfaced to the caller as an error string."""


@dataclass
class ToolContext:
    """Everything the tools read or write, injected once so the tools stay pure functions.

    The server builds one of these against the live run (state from the bus, KPIs from the
    store); the exercise script builds one against canned providers. Either way the tools are
    identical.
    """

    model: PreparedModel
    state_provider: Callable[[], BuildingState | None]
    forecast_provider: Callable[[int], list[ForecastPoint]]
    kpi_provider: Callable[[datetime | None], KpiSnapshot]
    guardian: Guardian = field(default_factory=Guardian)
    plan_slot: PlanSlot | None = None
    baseline: dict[tuple[str, str], float] = field(default_factory=dict)
    err_path: Path | None = None
    versions_dir: Path | None = None
    idf_path: Path | None = None
    install_dir: Path | None = None
    #: Commit an approved (non-fallback, non-rejected) plan to the slot for the executor?
    commit_on_submit: bool = True
    submit_count: int = 0


# --------------------------------------------------------------------------------------
# Read-only observation
# --------------------------------------------------------------------------------------


def get_state(ctx: ToolContext) -> BuildingState:
    """Current per-zone conditions plus outdoor weather and facility power."""
    state = ctx.state_provider()
    if state is None:
        raise ToolError("no observation available yet (simulation not past warmup)")
    return state


def get_forecasts(ctx: ToolContext, hours: int = 6) -> list[ForecastPoint]:
    """Look-ahead weather, tariff and grid carbon for the next ``hours``."""
    hours = max(1, min(24, hours))
    return ctx.forecast_provider(hours)


def get_kpis(ctx: ToolContext, since: datetime | None = None) -> KpiSnapshot:
    """Energy, cost, carbon, peak demand and comfort violations for the run so far."""
    return ctx.kpi_provider(since)


def read_error_log(ctx: ToolContext, lines: int = 50) -> str:
    """Return only the Severe/Fatal lines of the EnergyPlus ``.err`` file, at most ``lines``."""
    lines = max(1, min(50, lines))
    if ctx.err_path is None or not Path(ctx.err_path).is_file():
        return "(no .err file yet)"
    text = Path(ctx.err_path).read_text(encoding="utf-8", errors="replace")
    hits = [
        line.strip()
        for line in text.splitlines()
        if any(marker in line.lower() for marker in _ERR_SEVERE_MARKERS)
    ]
    if not hits:
        return "(no Severe or Fatal errors)"
    return "\n".join(hits[-lines:])


# --------------------------------------------------------------------------------------
# Mutating (still no actuation - the guardian is the only gate)
# --------------------------------------------------------------------------------------


def submit_plan(ctx: ToolContext, plan: Plan) -> ApprovedPlan:
    """Validate, guardian-filter, and return the verdict for ``plan``. Never actuates.

    Runs the identical ``guardian.core.Guardian`` the executor runs, against the current
    observed state, so a plan gets the same clip/rate/strip verdict here as on the internal
    path. On an accepted/clamped verdict (and if the context allows) the lowered plan is
    committed to the plan slot, where the executor re-filters it every timestep - the guardian
    is still the gate, this only deposits the candidate.
    """
    ctx.submit_count += 1
    state = ctx.state_provider()
    if state is None:
        raise ToolError("cannot review a plan before the first observation")

    now = state.sim_time
    setpoints = plan.to_setpoint_plan(now=now, baseline=ctx.baseline)
    history = RateHistory.empty()
    verdicts = [ctx.guardian.filter(setpoints, zone, history) for zone in state.zones]
    approved = ctx.guardian.approve(verdicts, plan_id=plan.plan_id, now=now)

    if (
        ctx.commit_on_submit
        and ctx.plan_slot is not None
        and approved.decision is not GuardianDecision.REJECTED
    ):
        # Deposit the *candidate* (raw lowered plan). The executor is the authoritative pass.
        ctx.plan_slot.commit(setpoints, at=now)

    log.info(
        "submit_plan %s -> %s (%d/%d steps, %d reasons)",
        plan.plan_id,
        approved.decision,
        len(approved.steps),
        len(setpoints.steps),
        sum(len(v.reasons) for v in verdicts),
    )
    return approved


def patch_model(ctx: ToolContext, spec: PatchSpec) -> str:
    """Apply a versioned IDF patch, auto-rolling back if the result does not parse.

    Returns a human-readable result string (new version path on success, error on rejection).
    Uses the Session 3 primitive, which validates the patched IDF *before* accepting it, so a
    patch that would not parse never enters the version series - the head is left untouched,
    which is the rollback.
    """
    from simulation.patching import PatchError, PatchValidationError, apply_patch

    if ctx.versions_dir is None or ctx.idf_path is None:
        raise ToolError("patch_model needs versions_dir and idf_path configured")

    try:
        new_path = apply_patch(
            ctx.idf_path,
            spec,
            versions_dir=ctx.versions_dir,
            install_dir=ctx.install_dir,
        )
    except PatchValidationError as exc:
        log.warning("patch %s rejected (unparseable); head unchanged: %s", spec.patch_id, exc)
        return f"rejected: patched model did not parse; no version written ({exc})"
    except PatchError as exc:
        log.warning("patch %s failed: %s", spec.patch_id, exc)
        return f"error: {exc}"

    return f"applied: {new_path.name} ({len(spec.operations)} op(s))"


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
    "ToolContext",
    "ToolError",
    "get_forecasts",
    "get_kpis",
    "get_state",
    "patch_model",
    "read_error_log",
    "submit_plan",
]
