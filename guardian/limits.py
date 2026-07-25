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


# --------------------------------------------------------------------------------------
# Configuration for the core filter (guardian/core.py)
# --------------------------------------------------------------------------------------
#
# `GuardianLimits` above is the older per-actuator envelope used by `guardian/supervisor.py`.
# The core filter this session builds uses a comfort envelope selected by *observed occupancy*,
# so its config is shaped differently: a centre + half-band for occupied hours, a wider ECM
# band for unoccupied hours, an explicit rate policy, and an actuator whitelist. Kept here so
# every guardian knob lives in one reviewable file.


class EnvelopeConfig(BaseModel):
    """Comfort envelope, selected at runtime by observed occupancy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Occupied hours: cooling setpoints held to ``centre ± half_band`` (default 23 ± 1.5).
    occupied_centre_c: float = Field(default=23.0)
    occupied_half_band_c: float = Field(default=1.5, gt=0)

    #: Fanger PMV comfort target and tolerance. The guardian will not let a plan push observed
    #: PMV further outside ``target ± tolerance``; it does not chase a PMV setpoint (that is the
    #: planner's job - the guardian only refuses to make discomfort worse).
    pmv_target: float = Field(default=0.0)
    pmv_tolerance: float = Field(default=0.5, gt=0)

    #: Unoccupied hours: a wider energy-conservation band (default 20-30 C).
    unoccupied_min_c: float = Field(default=20.0)
    unoccupied_max_c: float = Field(default=30.0)

    @property
    def occupied_min_c(self) -> float:
        return self.occupied_centre_c - self.occupied_half_band_c

    @property
    def occupied_max_c(self) -> float:
        return self.occupied_centre_c + self.occupied_half_band_c

    def band(self, *, occupied: bool) -> tuple[float, float]:
        """The ``(min, max)`` cooling-setpoint band in force for this occupancy."""
        if occupied:
            return (self.occupied_min_c, self.occupied_max_c)
        return (self.unoccupied_min_c, self.unoccupied_max_c)


class RateLimitConfig(BaseModel):
    """How fast a setpoint may move. Enforced statefully via an explicit ``RateHistory``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_step_per_timestep_c: float = Field(default=1.0, gt=0)
    max_step_per_hour_c: float = Field(default=2.0, gt=0)


class GuardianConfig(BaseModel):
    """The full policy for the core filter. Data, not code - a reviewable diff to change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope: EnvelopeConfig = Field(default_factory=EnvelopeConfig)
    rate: RateLimitConfig = Field(default_factory=RateLimitConfig)

    #: The actuators the guardian will pass through. Anything else is stripped and logged,
    #: never fatal. Defaults to exactly what the bus can drive today (the setpoint schedules);
    #: a plan asking for a fan or lighting actuator is stripped until that path is wired.
    whitelist: tuple[Actuator, ...] = (
        Actuator.COOLING_SETPOINT_C,
        Actuator.HEATING_SETPOINT_C,
    )

    def permits(self, actuator: Actuator) -> bool:
        return actuator in self.whitelist


DEFAULT_CONFIG = GuardianConfig()

__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_LIMITS",
    "Bound",
    "EnvelopeConfig",
    "GuardianConfig",
    "GuardianLimits",
    "RateLimitConfig",
]
