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
from pathlib import Path
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
    pmv: float | None = Field(
        default=None,
        description="Fanger PMV. Reported per People object, not per zone - see PreparedModel.",
    )


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
# Model wiring
# --------------------------------------------------------------------------------------


class ZoneBinding(_Base):
    """How one zone is wired into the runtime API.

    Two indirections that bite if you guess them:

    * Setpoints are actuated through the **schedule** the thermostat references, not through a
      thermostat actuator. ``simulation/prepare_idf.py`` rewrites those schedules to
      ``Schedule:Constant`` so ``Schedule:Constant / Schedule Value`` is writable.
    * Fanger PMV is reported keyed by the **People object name**, not the zone name. ``people``
      is that key; without it there is no PMV for this zone.
    """

    zone: str
    heating_schedule: str | None = None
    cooling_schedule: str | None = None
    people: str | None = None


class PreparedModel(_Base):
    """Index of an agent-ready IDF: written by ``prepare_idf``, read by the bus.

    Persisted next to the IDF so the two stages stay decoupled - the bus never re-parses the
    IDF, and a mismatch between them is a loud missing-key error rather than a silent -1
    handle at timestep 4000.
    """

    idf_path: str
    zones: list[ZoneBinding] = Field(default_factory=list)
    constant_schedules: dict[str, float] = Field(
        default_factory=dict,
        description="Schedule:Constant name -> the baseline value prepare_idf initialised it to.",
    )

    def binding(self, zone: str) -> ZoneBinding | None:
        """Return the binding for ``zone``, or ``None`` if the zone is not in the model."""
        for item in self.zones:
            if item.zone == zone:
                return item
        return None

    @property
    def zone_names(self) -> list[str]:
        return [z.zone for z in self.zones]

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: Path | str) -> PreparedModel:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


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
    "PreparedModel",
    "Violation",
    "ViolationCode",
    "ZoneBinding",
    "ZoneState",
]
