"""Run the baseline (control-arm) simulation: no agent, no actuation.

This is the A/B control arm *and* the fallback behaviour, which is deliberate - "agent
disabled" and "agent failed" must produce identical building behaviour.

The callback registered here is the one CLAUDE.md rule R1 is about. Even in the baseline case
its body is wrapped in ``try/except`` so a Python-side bug can never take the simulation down
across the C boundary.

Scaffold only: no logic yet.
"""

from __future__ import annotations

from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger

log = get_logger("simulation.baseline")


def run(settings: Settings | None = None) -> Path:
    """Run ``baseline.idf`` against the configured weather file.

    Returns the output directory. Requires a real EnergyPlus install - see
    :func:`common.eplus_path.require_energyplus`.
    """
    settings = settings or Settings.from_env()
    install_dir = eplus_path.require_energyplus()
    log.info("energyplus=%s idf=%s epw=%s", install_dir, settings.idf_path, settings.epw_path)
    raise NotImplementedError("baseline run not implemented yet (scaffold)")


def main() -> None:
    """CLI entry point: ``python -m simulation.run_baseline``."""
    log.info("starting baseline run")
    run()


if __name__ == "__main__":
    main()
