"""Make the EnergyPlus-bundled ``pyenergyplus`` package importable.

``pyenergyplus`` is **not** on PyPI. It ships inside the EnergyPlus installation directory
(alongside ``energyplus.exe`` / the ``libenergyplusapi`` shared library), so the only way to
import it is to put that directory on ``sys.path``.

Set ``ENERGYPLUS_DIR`` in the environment, then import this module *before* anything that
touches the runtime API::

    from common import eplus_path  # noqa: F401  (side effect: sys.path)
    from pyenergyplus.api import EnergyPlusAPI

Importing this module is deliberately non-fatal when EnergyPlus is absent - CI has no
EnergyPlus install and must still be able to import the package tree. Call
:func:`require_energyplus` at the point where the runtime API is genuinely needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_VAR = "ENERGYPLUS_DIR"

#: Set at import time to the directory that was added to ``sys.path``, else ``None``.
RESOLVED_DIR: Path | None = None


class EnergyPlusNotFoundError(RuntimeError):
    """Raised when the EnergyPlus install directory cannot be resolved."""


def energyplus_dir() -> Path | None:
    """Return the configured EnergyPlus install directory, or ``None`` if unset/missing."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_dir() else None


def ensure_on_path() -> Path | None:
    """Append ``$ENERGYPLUS_DIR`` to ``sys.path`` if it resolves. Idempotent.

    Returns the directory that is now on the path, or ``None`` if it could not be resolved.
    Never raises: a missing EnergyPlus is a valid state for lint, tests and the dashboard.
    """
    global RESOLVED_DIR

    directory = energyplus_dir()
    if directory is None:
        return None

    entry = str(directory)
    if entry not in sys.path:
        sys.path.append(entry)

    RESOLVED_DIR = directory
    return directory


def require_energyplus() -> Path:
    """Like :func:`ensure_on_path`, but raise if EnergyPlus is not available.

    Call this from simulation entry points - not at module import time.
    """
    directory = ensure_on_path()
    if directory is None:
        raise EnergyPlusNotFoundError(
            f"{ENV_VAR} is unset or does not point at a directory. "
            "Install EnergyPlus 24.x and set it to the install root, e.g. "
            "C:/EnergyPlusV24-1-0 or /usr/local/EnergyPlus-24-1-0. "
            "`pyenergyplus` cannot be pip-installed."
        )
    return directory


# Side effect on import - the documented purpose of this module.
ensure_on_path()
