"""Endurance run: a full simulated year with the agent live.

What this is actually testing is not efficiency but *survival* - that thousands of planning
cycles, some of which will fail, never take the simulation down, and that the guardian's
intervention rate stays bounded. A run that finishes on fallback is a pass with a caveat; a
run that crashes is a failure.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from pathlib import Path


def run_endurance(days: int = 365, *, output_dir: Path | None = None) -> dict[str, float]:
    """Run the long simulation and return survival statistics.

    Reported: planning cycles attempted, planner failures, guardian clamps, guardian
    rejections, fallback timesteps, and wall-clock.
    """
    raise NotImplementedError("endurance run not implemented yet (scaffold)")


__all__ = ["run_endurance"]
