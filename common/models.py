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

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.generated_enums import ActuatorEnum, ZoneEnum

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


class TriggerEnum(StrEnum):
    """Why a planning cycle ran. ``EVENT`` is the Session 6 event-detector hook."""

    STARTUP = "startup"
    HOURLY = "hourly"
    EVENT = "event"


class EcmEnum(StrEnum):
    """The energy-conservation-measure playbook the planner may invoke.

    A fixed, closed vocabulary the LLM is constrained to - not free text - so a plan's strategy
    is machine-readable on the dashboard and in the journal. The system prompt describes when
    each applies; the guardian does not act on these (it acts on the resulting setpoints), they
    are the planner's declared intent.
    """

    PRECOOL = "precool"  # pull temperature down before a price/carbon peak
    COAST = "coast"  # let temperature drift within comfort to ride out a peak
    SETPOINT_RELAX = "setpoint_relax"  # widen setpoints during unoccupied/low-value hours
    NIGHT_PURGE = "night_purge"  # use cool night air to pre-cool the mass
    LOAD_SHIFT = "load_shift"  # move cooling work to cheaper/cleaner hours
    PEAK_LIMIT = "peak_limit"  # cap demand during the evening peak
    HOLD = "hold"  # no change; the baseline is already the right call


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
#
# Two levels of abstraction, both Pydantic, both here (rule R4):
#
# * `Plan` / `PlanAction` - what the **LLM** emits. Rich and human-meaningful: enum-typed zone
#   and actuator (codegen'd from the IDF), a value window (start/end), a per-action rationale,
#   an ECM playbook and a trigger. `Plan.model_json_schema()` is the constrained-decoding
#   grammar handed to Ollama.
# * `SetpointPlan` / `PlanStep` - what the **guardian and executor** operate on. Flat,
#   relative-time setpoint moves. The scheduler lowers a `Plan` to a `SetpointPlan`
#   (`Plan.to_setpoint_plan`) before committing it to the plan slot.
#
# `ApprovedPlan` (further down) is the actuation-authorised form the bus accepts; only the
# guardian builds one.


class PlanAction(_Base):
    """One setpoint intent over a time window - the unit of an LLM :class:`Plan`.

    ``zone`` and ``actuator`` are enums generated from the IDF, so the model is constrained at
    decode time to name only things that exist. ``value`` carries a coarse physical sanity bound
    here; the *real*, per-actuator envelope is the guardian's job, not the schema's.
    """

    zone: ZoneEnum
    actuator: ActuatorEnum
    value: float = Field(ge=-10.0, le=100.0)
    start: datetime
    end: datetime
    rationale: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def _check_window(self) -> PlanAction:
        if self.end < self.start:
            raise ValueError("action end must be at or after start")
        return self


class Plan(_Base):
    """What the LLM produces. Untrusted until the guardian says otherwise.

    ``Plan.model_json_schema()`` is the constrained-decoding grammar handed to Ollama - keep it
    small, flat and free of exotic types. Lower it to the guardian's representation with
    :meth:`to_setpoint_plan`.
    """

    plan_id: str = Field(default_factory=_new_id)
    schema_version: int = Field(default=SCHEMA_VERSION)
    created_at: datetime = Field(default_factory=_now)
    planner_model: str = Field(default="")
    trigger: TriggerEnum = Field(default=TriggerEnum.STARTUP)
    horizon_hours: int = Field(default=6, ge=4, le=6)
    actions: list[PlanAction] = Field(default_factory=list, max_length=64)
    ecms: list[EcmEnum] = Field(default_factory=list, max_length=8)
    confidence: float | None = Field(default=None, ge=0, le=1)

    def to_setpoint_plan(
        self,
        *,
        now: datetime,
        baseline: dict[tuple[str, str], float] | None = None,
    ) -> SetpointPlan:
        """Lower this LLM plan into the flat, relative-time form the guardian consumes.

        Each action becomes a :class:`PlanStep` at ``start`` (offset from ``now``). When a
        ``baseline`` (``{(zone, actuator): value}``) is supplied, a second step at ``end``
        reverts the setpoint - so an action's window actually closes instead of holding forever.
        Offsets are clamped to ``[0, 24h]``; ``now`` is *simulation* time, not wall-clock.
        """
        steps: list[PlanStep] = []
        for action in self.actions:
            zone = str(action.zone.value)
            actuator = Actuator(action.actuator.value)
            start_offset = _offset_minutes(action.start, now)
            steps.append(
                PlanStep(
                    offset_minutes=start_offset, zone=zone, actuator=actuator, value=action.value
                )
            )
            if baseline is not None:
                revert = baseline.get((zone, str(action.actuator.value)))
                end_offset = _offset_minutes(action.end, now)
                if revert is not None and end_offset > start_offset:
                    steps.append(
                        PlanStep(
                            offset_minutes=end_offset, zone=zone, actuator=actuator, value=revert
                        )
                    )

        return SetpointPlan(
            plan_id=self.plan_id,
            created_at=self.created_at,
            planner_model=self.planner_model,
            horizon_minutes=self.horizon_hours * 60,
            steps=steps,
            rationale="; ".join(a.rationale for a in self.actions if a.rationale)[:1000],
        )


class PlanStep(_Base):
    """A single setpoint move, relative to the moment the plan was issued.

    ``offset_minutes`` is relative rather than absolute so that a plan stays meaningful if it
    is applied a timestep late - and so nothing downstream has to reason about wall-clock.
    """

    offset_minutes: int = Field(ge=0, le=24 * 60)
    zone: str = Field(min_length=1)
    actuator: Actuator
    value: float


class SetpointPlan(_Base):
    """The guardian/executor representation: flat, relative-time setpoint moves.

    Produced by lowering an LLM :class:`Plan`, or built directly by harnesses that actuate
    without a model (the smoke tests). Distinct from :class:`Plan` so the planner-facing schema
    and the actuation-facing schema can evolve independently.
    """

    plan_id: str = Field(default_factory=_new_id)
    schema_version: int = Field(default=SCHEMA_VERSION)
    created_at: datetime = Field(default_factory=_now)
    planner_model: str = Field(default="")
    horizon_minutes: int = Field(default=60, gt=0, le=24 * 60)
    steps: list[PlanStep] = Field(default_factory=list, max_length=64)
    rationale: str = Field(default="", max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)


def _offset_minutes(when: datetime, now: datetime) -> int:
    """Minutes from ``now`` to ``when``, clamped to a single day's worth of offset."""
    delta = (when - now).total_seconds() / 60.0
    return max(0, min(24 * 60, round(delta)))


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


class GuardianStatus(StrEnum):
    """Outcome of a single :meth:`guardian.core.Guardian.filter` call.

    Distinct from :class:`GuardianDecision` on purpose. ``GuardianDecision`` is the older
    vocabulary carried by :class:`ApprovedPlan` on the live-bus path; ``GuardianStatus`` is the
    per-zone filter verdict, and its wording (``clipped``, not ``clamped``) matches the machine
    reason strings the planner is fed verbatim in a later session. The executor maps a set of
    per-zone statuses onto an ``ApprovedPlan``'s ``GuardianDecision`` when it assembles them.
    """

    ACCEPTED = "accepted"
    CLIPPED = "clipped"
    REJECTED = "rejected"


class GuardianVerdict(_Base):
    """What :meth:`guardian.core.Guardian.filter` returns for one zone.

    ``reasons`` are short, stable, machine-usable strings - ``"clip: Z2 24.9->24.5
    envelope_max"``, ``"rate: Z2 21.5->23 rate_step"``, ``"strip: Z2 fan_flow_fraction
    whitelist"``. They are logged, surfaced on the dashboard, and (later) fed back to the
    planner verbatim so it can learn what the guardian will not allow. Keep the grammar stable:
    ``<action>: <zone> <orig>-><new> <rule>`` for clips/rates, ``strip: <zone> <actuator>
    whitelist`` for strips.

    ``safe_plan`` is a :class:`SetpointPlan` (not an :class:`ApprovedPlan`) holding only the
    steps that survived filtering for this zone. Turning it into the ``ApprovedPlan`` the
    actuator accepts is the guardian's job (rule R2) - see :meth:`guardian.core.Guardian.approve`.
    """

    status: GuardianStatus
    zone: str
    reasons: list[str] = Field(default_factory=list)
    safe_plan: SetpointPlan

    @property
    def accepted(self) -> bool:
        return self.status is GuardianStatus.ACCEPTED

    @property
    def clipped(self) -> bool:
        return self.status is GuardianStatus.CLIPPED

    @property
    def rejected(self) -> bool:
        return self.status is GuardianStatus.REJECTED


# --------------------------------------------------------------------------------------
# Model mutation: patches and version snapshots
# --------------------------------------------------------------------------------------
#
# Competition deliverable #2 is "baseline + runtime-modified .idf files". These types are the
# contract for producing that series: `AppliedControlState` is what a snapshot captures,
# `PatchSpec` is a structural edit, and `SnapshotManifest` is the index tying them together.


class PatchOp(StrEnum):
    """The closed set of structural edits ``patch_model`` may make to an IDF."""

    SET_FIELD = "set_field"
    ADD_OBJECT = "add_object"
    REMOVE_OBJECT = "remove_object"


class PatchOperation(_Base):
    """One edit against one IDF object.

    Deliberately narrow. An LLM-driven ``patch_model`` with a free-form "edit the IDF" surface
    can do arbitrary damage; three verbs against a named object is something the validator can
    actually reason about, and something a reviewer can read in a diff.
    """

    op: PatchOp
    object_type: str = Field(min_length=1, description="IDF class, e.g. 'SCHEDULE:CONSTANT'.")
    object_name: str = Field(min_length=1)
    field: str | None = Field(default=None, description="eppy field name, for set_field.")
    value: float | str | None = None
    fields: dict[str, float | str] = Field(
        default_factory=dict, description="Field name -> value, for add_object."
    )

    @model_validator(mode="after")
    def _check_shape(self) -> PatchOperation:
        if self.op is PatchOp.SET_FIELD:
            if not self.field:
                raise ValueError("set_field requires 'field'")
            if self.value is None:
                raise ValueError("set_field requires 'value'")
        elif self.op is PatchOp.ADD_OBJECT and not self.fields:
            raise ValueError("add_object requires 'fields'")
        return self

    def describe(self) -> str:
        if self.op is PatchOp.SET_FIELD:
            return f"set {self.object_type}[{self.object_name}].{self.field} = {self.value}"
        if self.op is PatchOp.ADD_OBJECT:
            return f"add {self.object_type}[{self.object_name}] ({len(self.fields)} fields)"
        return f"remove {self.object_type}[{self.object_name}]"


class PatchSpec(_Base):
    """A reviewable, rollback-safe bundle of IDF edits.

    Applied atomically: the patched model must parse before the new version is accepted, so a
    spec either lands whole or not at all. This is the payload of the MCP ``patch_model`` tool.
    """

    patch_id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_now)
    reason: str = Field(default="", max_length=500)
    operations: list[PatchOperation] = Field(min_length=1, max_length=64)

    def describe(self) -> list[str]:
        return [operation.describe() for operation in self.operations]


class AppliedControlState(_Base):
    """The control state actually in force at a moment in simulation time.

    This is what a snapshot materialises into an IDF. It is intentionally *not* a plan: a plan
    is an intention over a horizon, whereas this is the flat set of values in force right now,
    which is the only thing an IDF can express.
    """

    sim_time: datetime
    schedule_values: dict[str, float] = Field(default_factory=dict)
    applied_patches: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    trigger: str = Field(default="plan_commit", max_length=64)

    def content_hash(self) -> str:
        """Stable hash of the *control-relevant* content only.

        Excludes ``sim_time`` and ``trigger`` on purpose: two commits with identical setpoints
        at different times are the same model, and writing the second one would pad the version
        series with noise. Values are rounded before hashing so float representation drift
        cannot manufacture a spurious new version.
        """
        payload = {
            "schedules": {k: round(float(v), 4) for k, v in sorted(self.schedule_values.items())},
            "patches": sorted(self.applied_patches),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def diff_against(self, previous: AppliedControlState | None) -> list[str]:
        """Human-readable summary of what changed since ``previous``."""
        if previous is None:
            return [f"initial: {len(self.schedule_values)} schedule(s)"]

        lines: list[str] = []
        for name in sorted(set(self.schedule_values) | set(previous.schedule_values)):
            before = previous.schedule_values.get(name)
            after = self.schedule_values.get(name)
            if before is None:
                lines.append(f"+{name} = {after:.2f}")
            elif after is None:
                lines.append(f"-{name} (was {before:.2f})")
            elif round(before, 4) != round(after, 4):
                lines.append(f"{name}: {before:.2f} -> {after:.2f}")

        for patch in sorted(set(self.applied_patches) - set(previous.applied_patches)):
            lines.append(f"+patch {patch}")
        for patch in sorted(set(previous.applied_patches) - set(self.applied_patches)):
            lines.append(f"-patch {patch}")

        return lines or ["no control-relevant change"]


class SnapshotEntry(_Base):
    """One row of the version manifest - one materialised ``.idf`` on disk."""

    version: int = Field(ge=1)
    path: str
    content_hash: str
    sim_time: datetime
    trigger: str
    plan_id: str | None = None
    diff_summary: list[str] = Field(default_factory=list)
    state: AppliedControlState


class SnapshotManifest(_Base):
    """The index of the generated model series - competition deliverable #2."""

    base_idf: str = ""
    entries: list[SnapshotEntry] = Field(default_factory=list)

    @property
    def head(self) -> SnapshotEntry | None:
        return self.entries[-1] if self.entries else None

    @property
    def next_version(self) -> int:
        return max((e.version for e in self.entries), default=0) + 1

    def entry(self, version: int) -> SnapshotEntry | None:
        for item in self.entries:
            if item.version == version:
                return item
        return None

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: Path | str) -> SnapshotManifest:
        target = Path(path)
        if not target.is_file():
            return cls()
        return cls.model_validate_json(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# The control interface
# --------------------------------------------------------------------------------------


@runtime_checkable
class ControlInterface(Protocol):
    """What the agent talks to, whichever actuation mode is in force.

    ``agent.bus.SimulationBus`` (live actuation) and
    ``simulation.receding.RecedingHorizonDriver`` (re-simulation) both satisfy this. Agent code
    depends on this shape and nothing more, so switching modes is a construction-site change
    rather than a code change - which is the whole point of having a contingency mode.

    ``state`` is an opaque, mode-specific token: the EnergyPlus state handle in live mode,
    unused in receding mode. Callers that are not inside a callback omit it.
    """

    def read_state(self, state: object | None = ...) -> BuildingState | None:
        """Latest observation, or ``None`` when nothing is observable yet."""
        ...

    def write_setpoints(
        self, approved: ApprovedPlan, *, now: datetime, state: object | None = ...
    ) -> int:
        """Apply a guardian-approved plan. Returns the number of values written."""
        ...


__all__ = [
    "SCHEMA_VERSION",
    "Actuator",
    "ActuatorEnum",
    "AppliedControlState",
    "ApprovedPlan",
    "BuildingState",
    "ControlInterface",
    "EcmEnum",
    "ForecastPoint",
    "GuardianDecision",
    "GuardianEvent",
    "GuardianStatus",
    "GuardianVerdict",
    "KpiSnapshot",
    "PatchOp",
    "PatchOperation",
    "PatchSpec",
    "Plan",
    "PlanAction",
    "PlanStep",
    "PreparedModel",
    "SetpointPlan",
    "SnapshotEntry",
    "SnapshotManifest",
    "TriggerEnum",
    "Violation",
    "ViolationCode",
    "ZoneBinding",
    "ZoneEnum",
    "ZoneState",
]
