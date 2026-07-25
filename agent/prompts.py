"""Prompt templates for the planner.

Anything model-specific lives here, so that swapping the Ollama model stays a config change.
The JSON schema is not pasted into the prompt as prose - it is passed to Ollama as a
constrained-decoding grammar derived from ``Plan.model_json_schema()``. The prompt only has to
explain *intent*.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are the planning layer of a building-energy control system.

You are given a digest: recent zone conditions, weather and price/carbon forecasts, and
current KPIs. You propose setpoint moves over the next planning horizon that reduce cost and
carbon while keeping occupied zones inside their comfort band.

Constraints you must respect:
- Only the actuators listed in the schema exist. Do not invent zones or actuators.
- Offsets are minutes from now, never wall-clock times.
- Prefer few, meaningful moves over many small ones.
- A deterministic safety guardian reviews everything you emit and will clamp or reject it.
  Do not attempt to work around it; unsafe plans are discarded whole and cost you the cycle.

Answer with a plan object only.
"""

# TODO(scaffold): digest rendering is not implemented yet - see agent/digest.py.
USER_PROMPT_TEMPLATE = """\
Current simulated time: {sim_time}

## State
{state_block}

## Forecast (next {horizon_hours} h)
{forecast_block}

## KPIs so far
{kpi_block}

Propose a plan for the next {horizon_minutes} minutes.
"""

__all__ = ["SYSTEM_PROMPT", "USER_PROMPT_TEMPLATE"]
