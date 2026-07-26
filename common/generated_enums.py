"""Zone and actuator enums for the planner contract - CODE-GENERATED from the IDF.

**Do not hand-edit.** ``simulation/prepare_idf.py`` regenerates this file from the actual
prepared model (``agentic_model.json``) so that ``ZoneEnum`` contains exactly the zones the
building has and ``ActuatorEnum`` exactly the actuators the bus can drive. Regenerate with::

    python -m simulation.prepare_idf

Why generated rather than hand-written: these enums are the *closed vocabulary* the LLM is
constrained to during decoding (``Plan.model_json_schema()`` embeds them). If the model could
name a zone the building does not have, the guardian would strip it and the planning cycle
would be wasted. Codegen from the IDF makes "the planner can only name real zones" true by
construction.

The values committed here are a **default** for a DOE small-office prototype, present so the
package imports on a machine with no IDF prepared yet (CI, tests, the dashboard). They are
overwritten the moment ``prepare_idf`` runs against the real model.
"""

from __future__ import annotations

from enum import StrEnum

#: Provenance marker. ``prepare_idf`` stamps the source model here; a value starting with
#: "default" means the file has not been regenerated from a real IDF yet.
GENERATED_FROM = "default (DOE small office; regenerate with `python -m simulation.prepare_idf`)"


class ZoneEnum(StrEnum):
    """Conditioned thermal zones. Values match the IDF zone names exactly."""

    CORE_ZN = "Core_ZN"
    PERIMETER_ZN_1 = "Perimeter_ZN_1"
    PERIMETER_ZN_2 = "Perimeter_ZN_2"
    PERIMETER_ZN_3 = "Perimeter_ZN_3"
    PERIMETER_ZN_4 = "Perimeter_ZN_4"


class ActuatorEnum(StrEnum):
    """Actuators the planner may address. A strict subset of ``common.models.Actuator``.

    Only what the bus can actually drive today (the setpoint schedules). Values are identical
    to the matching ``Actuator`` members, so lowering a ``PlanAction`` to a ``PlanStep`` is a
    plain ``Actuator(action.actuator.value)``.
    """

    COOLING_SETPOINT_C = "cooling_setpoint_c"
    HEATING_SETPOINT_C = "heating_setpoint_c"


__all__ = ["GENERATED_FROM", "ActuatorEnum", "ZoneEnum"]
