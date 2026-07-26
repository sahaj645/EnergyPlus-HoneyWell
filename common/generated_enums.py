"""Zone and actuator enums for the planner contract - CODE-GENERATED from the IDF.

**Do not hand-edit.** Regenerate with `python -m simulation.prepare_idf`.
These are the closed vocabulary the LLM is constrained to during decoding.
"""

from __future__ import annotations

from enum import StrEnum

GENERATED_FROM = 'D:\\Energyplus_Honewell\\simulation\\agentic.idf'


class ZoneEnum(StrEnum):
    """Conditioned thermal zones. Values match the IDF zone names exactly."""

    CORE_ZN = "Core_ZN"
    PERIMETER_ZN_1 = "Perimeter_ZN_1"
    PERIMETER_ZN_2 = "Perimeter_ZN_2"
    PERIMETER_ZN_3 = "Perimeter_ZN_3"
    PERIMETER_ZN_4 = "Perimeter_ZN_4"


class ActuatorEnum(StrEnum):
    """Actuators the planner may address. A subset of `common.models.Actuator`."""

    COOLING_SETPOINT_C = "cooling_setpoint_c"
    HEATING_SETPOINT_C = "heating_setpoint_c"


__all__ = ["GENERATED_FROM", "ActuatorEnum", "ZoneEnum"]
