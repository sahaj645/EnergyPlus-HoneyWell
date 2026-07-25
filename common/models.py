"""The HIVE data contract - **single source of truth** (CLAUDE.md, rule R4).

`agent/`, `guardian/`, `mcp_server/`, the journal and the dashboard all import these models.
Nothing anywhere else may define a plan-shaped dict, TypedDict or parallel dataclass. If the
planner and the guardian can disagree about what a plan *is*, the guardian is not a safety
layer.

Two properties this module is designed around:

* ``Plan.model_json_schema()`` is fed straight to Ollama for constrained decoding, so the
  schema has to stay small, flat and free of exotic types.
* :class:`ApprovedPlan` is a *distinct type* from :class:`Plan`. The actuator signature
  accepts only an ``ApprovedPlan``, and only the guardian constructs one - that is what makes
  "no bypass path" (rule R2) checkable rather than aspirational.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid4().hex[:12]


class _Base(BaseModel):
    """Strict base: unknown keys are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------------------
# Actuation vocabulary
# --------------------------------------------------------------------------------------


class Actuator(StrEnum):
    """The closed set of things a plan is allowed to move.

    Anything not listed here cannot be expressed in a plan, which is the cheapest possible
    form of safety. Extending this set means extending the guardian's limits in the same
    commit.
    """

    COOLING_SETPOINT_C = "cooling_setpoint_c"
    HEATING_SETPOINT_C = "heating_setpoint_c"
    SUPPLY_AIR_TEMP_C = "supply_air_temp_c"
    FAN_FLOW_FRACTION = "fan_flow_fraction"
    LIGHTING_FRACTION = "lighting_fraction"


class GuardianDecision(StrEnum):
    """What the guardian did with a plan."""

    ACCEPTED = "accepted"
    CLAMPED = "clamped"
    REJECTED = "rejected"


class ViolationCode(StrEnum):
    """Why the guardian intervened. Stable strings - they are logged and charted."""

    OUT_OF_RANGE = "out_of_range"
    RATE_LIMIT = "rate_limit"
    COMFORT_BAND = "comfort_band"
    DEADBAND = "deadband"
    UNKNOWN_ZONE = "unknown_zone"
    STALE_PLAN = "stale_plan"
    SCHEMA_INVALID = "schema_invalid"
    WATCHDOG_TIMEOUT = "watchdog_timeout"


# --------------------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------------------


class ZoneState(_Base):
    """One thermal zone at one instant."""

    zone: str
    air_temp_c: float
    mean_radiant_temp_c: float | None = None
    relative_humidity: float | None = Field(default=None, ge=0, le=100)
    occupancy: float | None = Field(default=None, ge=0)
    cooling_setpoint_c: float | None = None
    heating_setpoint_c: float | None = None


class BuildingState(_Base):
    """A full observation, as handed to the digest builder."""

    sim_time: datetime
    outdoor_air_temp_c: float
    outdoor_relative_humidity: float | None = Field(default=None, ge=0, le=100)
    direct_normal_irradiance: float | None = Field(default=None, ge=0)
    facility_power_w: float = Field(ge=0)
    zones: list[ZoneState] = Field(default_factory=list)


class ForecastPoint(_Base):
    """One hour of look-ahead: weather, price and grid carbon."""

    timestamp: datetime
    outdoor_air_temp_c: float | None = None
    direct_normal_irradiance: float | None = Field(default=None, ge=0)
    tariff_inr_per_kwh: float | None = Field(default=None, ge=0)
    carbon_g_per_kwh: float | None = Field(default=None, ge=0)


class KpiSnapshot(_Base):
    """Scoreboard for a window of simulated time."""

    window_start: datetime
    window_end: datetime
    energy_kwh: float = Field(ge=0)
    cost_inr: float = Field(ge=0)
    carbon_kg: float = Field(ge=0)
    peak_demand_kw: float = Field(ge=0)
    comfort_violation_hours: float = Field(ge=0)
    unmet_load_hours: float = Field(default=0.0, ge=0)


# --------------------------------------------------------------------------------------
# The plan contract
# --------------------------------------------------------------------------------------


class PlanStep(_Base):
    """A single setpoint move, relative to the moment the plan was issued.

    ``offset_minutes`` is relative rather than absolute so that a plan stays meaningful if it
    is applied a timestep late - and so the planner never has to reason about wall-clock.
    """

    offset_minutes: int = Field(ge=0, le=24 * 60)
    zone: str = Field(min_length=1)
    actuator: Actuator
    value: float


class Plan(_Base):
    """What the LLM produces. Untrusted until the guardian says otherwise.

    ``Plan.model_json_schema()`` is the constrained-decoding grammar handed to Ollama.
    """

    plan_id: str = Field(default_factory=_new_id)
    schema_version: int = Field(default=SCHEMA_VERSION)
    created_at: datetime = Field(default_factory=_now)
    planner_model: str = Field(default="")
    horizon_minutes: int = Field(default=60, gt=0, le=24 * 60)
    steps: list[PlanStep] = Field(default_factory=list, max_length=64)
    rationale: str = Field(default="", max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)


class Violation(_Base):
    """One guardian intervention against one step (or the plan as a whole)."""

    code: ViolationCode
    message: str
    step_index: int | None = None
    original_value: float | None = None
    clamped_value: float | None = None


class ApprovedPlan(_Base):
    """The only object the actuator accepts. Constructed **exclusively** by the guardian.

    Distinct from :class:`Plan` on purpose: there is no code path that turns raw model output
    into actuation without passing through the guardian (rule R2).
    """

    plan_id: str
    approved_at: datetime = Field(default_factory=_now)
    decision: GuardianDecision
    steps: list[PlanStep] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    fallback: bool = Field(
        default=False,
        description="True when these steps are the baseline schedule, not the planner's.",
    )


class GuardianEvent(_Base):
    """Journal row: what the guardian saw and what it decided. Written for every plan."""

    event_id: str = Field(default_factory=_new_id)
    at: datetime = Field(default_factory=_now)
    plan_id: str
    decision: GuardianDecision
    violations: list[Violation] = Field(default_factory=list)
    note: str = ""


__all__ = [
    "SCHEMA_VERSION",
    "Actuator",
    "ApprovedPlan",
    "BuildingState",
    "ForecastPoint",
    "GuardianDecision",
    "GuardianEvent",
    "KpiSnapshot",
    "Plan",
    "PlanStep",
    "Violation",
    "ViolationCode",
    "ZoneState",
]
