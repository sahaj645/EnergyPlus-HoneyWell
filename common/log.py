"""Logging setup for HIVE.

Named ``log`` rather than ``logging`` so that ``import logging`` inside this package is
unambiguous to a human reader.

One rule worth remembering: logging from inside the EnergyPlus callback is allowed, but only
at ``WARNING`` and above, and only to handlers that do not block (see CLAUDE.md, rule R1).
Per-timestep ``INFO`` logging belongs in the telemetry batch, not on the hot path.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup(level: str = "INFO") -> None:
    """Install a single stderr handler on the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    setup()
    return logging.getLogger(f"hive.{name}")
