"""Declarative safety envelope.

Limits are data, not code: they are declared here, versioned in git, and rendered on the
dashboard so a reviewer can see exactly what the agent is allowed to do. Widening one is a
reviewable diff.

Values below are placeholders for a warm-climate commercial office and will be tuned against
the actual IDF once ``simulation/baseline.idf`` lands.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from common.models import Actuator


class Bound(BaseModel):
    """Absolute range plus a per-planning-cycle rate limit for one actuator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum: float
    maximum: float
    max_step: float = Field(gt=0, description="Largest change permitted in one cycle.")


class GuardianLimits(BaseModel):
    """The full envelope. Passed to the supervisor; never mutated at runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bounds: dict[Actuator, Bound] = Field(
        default_factory=lambda: {
            Actuator.COOLING_SETPOINT_C: Bound(minimum=22.0, maximum=28.0, max_step=1.5),
            Actuator.HEATING_SETPOINT_C: Bound(minimum=16.0, maximum=22.0, max_step=1.5),
            Actuator.SUPPLY_AIR_TEMP_C: Bound(minimum=12.0, maximum=18.0, max_step=2.0),
            Actuator.FAN_FLOW_FRACTION: Bound(minimum=0.2, maximum=1.0, max_step=0.25),
            Actuator.LIGHTING_FRACTION: Bound(minimum=0.3, maximum=1.0, max_step=0.5),
        }
    )

    #: Minimum gap between heating and cooling setpoints, to stop them fighting.
    min_deadband_c: float = Field(default=2.0, gt=0)

    #: Occupied-hours comfort band. Breaching it is a KPI penalty, not just a warning.
    comfort_min_c: float = Field(default=22.0)
    comfort_max_c: float = Field(default=27.0)

    #: A plan older than this is treated as stale and the fallback takes over.
    max_plan_age_minutes: int = Field(default=30, gt=0)

    #: Zones the planner is allowed to address. Empty means "read from the model".
    allowed_zones: tuple[str, ...] = ()


DEFAULT_LIMITS = GuardianLimits()

__all__ = ["DEFAULT_LIMITS", "Bound", "GuardianLimits"]
