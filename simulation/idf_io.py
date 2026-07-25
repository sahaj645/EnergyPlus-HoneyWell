"""Shared eppy bootstrap.

``IDF.setiddname`` is process-global and only takes effect once - eppy ignores later calls
with a different path and silently keeps the first IDD. Four modules now load IDFs, so the
bootstrap lives here rather than being copy-pasted with four chances to disagree about which
IDD won.
"""

from __future__ import annotations

from pathlib import Path

from common import eplus_path
from common.log import get_logger

log = get_logger("simulation.idf_io")

_IDD_SET: Path | None = None


def idd_path(install_dir: Path | str | None = None) -> Path:
    """Locate ``Energy+.idd`` inside the EnergyPlus install."""
    root = Path(install_dir) if install_dir else eplus_path.require_energyplus()
    idd = root / "Energy+.idd"
    if not idd.is_file():
        raise FileNotFoundError(f"Energy+.idd not found at {idd}; is {root} an EnergyPlus root?")
    return idd


def load_idf(idf_path: Path | str, install_dir: Path | str | None = None):
    """Open an IDF with eppy. Imports eppy lazily so this module stays import-safe."""
    from eppy.modeleditor import IDF

    global _IDD_SET

    target = Path(idf_path)
    if not target.is_file():
        raise FileNotFoundError(f"{target} not found")

    idd = idd_path(install_dir)
    if _IDD_SET is None:
        IDF.setiddname(str(idd))
        _IDD_SET = idd
    elif _IDD_SET != idd:
        log.warning(
            "IDD already set to %s; ignoring request for %s (eppy allows one per process)",
            _IDD_SET,
            idd,
        )

    return IDF(str(target))


def save_idf(idf, out_path: Path | str) -> Path:
    """Write an eppy IDF to ``out_path``, creating parents."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    idf.saveas(str(out))
    return out


__all__ = ["idd_path", "load_idf", "save_idf"]
