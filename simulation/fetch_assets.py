"""Acquire the two simulation inputs: a prototype office IDF and a hot-humid EPW.

Neither is committed to the repo (licensing + size), so this fetches them into
``simulation/``. It tries the reliable sources first and, when a download cannot be made to
work, prints exact manual-download instructions rather than failing silently - a wrong or
missing asset should be obvious, not a mystery ``FileNotFoundError`` three scripts later.

Targets:

* ``simulation/baseline.idf`` - DOE prototype **small office** (ASHRAE 90.1, 5 zone).
  Most reliable source is the EnergyPlus install itself: ``$ENERGYPLUS_DIR/ExampleFiles``
  ships the DOE reference small-office model. We copy from there if available, else fall back
  to a download, else print instructions.
* ``simulation/weather.epw`` - a hot-humid Indian TMY file (Chennai, WMO 432790, or
  equivalent). These live on climate.onebuilding.org as per-station zips; the exact filename
  carries a data-year range that changes, so this usually resolves to manual instructions with
  the precise link and target path.

``--dry-run`` reports what it would do without touching the network or filesystem.
"""

from __future__ import annotations

import argparse
import io
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger

log = get_logger("simulation.fetch")

_USER_AGENT = "hive-fetch-assets/0.1 (+https://github.com/sahaj645/EnergyPlus-HoneyWell)"
_TIMEOUT_S = 15
_MIN_IDF_BYTES = 50_000
_MIN_EPW_BYTES = 500_000

# Glob patterns for a small-office reference model inside $ENERGYPLUS_DIR/ExampleFiles.
_EXAMPLE_IDF_PATTERNS = (
    "RefBldgSmallOfficeNew2004*.idf",
    "ASHRAE901_OfficeSmall*.idf",
    "*SmallOffice*.idf",
)

# Best-effort download URLs. These 404 from time to time as upstream reorganises; failure here
# is expected and handled by falling through to manual instructions.
_EPW_URLS = (
    "https://climate.onebuilding.org/WMO_Region_2_Asia/IND_India/TN_Tamil_Nadu/"
    "IND_TN_Chennai-Madras.Intl.AP.432790_TMYx.zip",
    "https://climate.onebuilding.org/WMO_Region_2_Asia/IND_India/TN_Tamil_Nadu/"
    "IND_TN_Chennai.Madras.Intl.AP.432790_ITMY.zip",
)


class AssetError(RuntimeError):
    """A required asset could not be acquired automatically."""


# --------------------------------------------------------------------------------------
# IDF
# --------------------------------------------------------------------------------------


def _find_example_idf(install_dir: Path) -> Path | None:
    example_dir = install_dir / "ExampleFiles"
    if not example_dir.is_dir():
        return None
    for pattern in _EXAMPLE_IDF_PATTERNS:
        matches = sorted(example_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def fetch_idf(target: Path, *, dry_run: bool = False) -> bool:
    """Put a prototype small-office IDF at ``target``. Returns True on success."""
    if target.is_file():
        log.info("IDF already present: %s", target)
        return True

    install_dir = eplus_path.energyplus_dir()
    source = _find_example_idf(install_dir) if install_dir else None

    if source is not None:
        log.info("%scopy IDF from EnergyPlus ExampleFiles: %s", _prefix(dry_run), source)
        if not dry_run:
            shutil.copyfile(source, target)
        return True

    log.info("no ExampleFiles IDF found and no reliable direct download for the DOE prototype")
    return False


# --------------------------------------------------------------------------------------
# EPW
# --------------------------------------------------------------------------------------


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310 (https only, our URL)
        return response.read()


def _epw_from_zip(blob: bytes) -> bytes | None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return None
    for name in archive.namelist():
        if name.lower().endswith(".epw"):
            return archive.read(name)
    return None


def fetch_epw(target: Path, *, dry_run: bool = False) -> bool:
    """Put a hot-humid Indian EPW at ``target``. Returns True on success."""
    if target.is_file():
        log.info("EPW already present: %s", target)
        return True

    if dry_run:
        log.info(
            "%stry %d EPW URL(s), extracting the .epw from each zip",
            _prefix(True),
            len(_EPW_URLS),
        )
        return False

    for url in _EPW_URLS:
        try:
            log.info("trying %s", url)
            blob = _download(url)
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("  failed: %s", exc)
            continue

        epw_bytes = _epw_from_zip(blob) if url.lower().endswith(".zip") else blob
        if epw_bytes is None or len(epw_bytes) < _MIN_EPW_BYTES:
            log.warning("  downloaded content did not look like an EPW (%s bytes)",
                        len(epw_bytes) if epw_bytes else 0)
            continue
        if not epw_bytes.lstrip().upper().startswith(b"LOCATION"):
            log.warning("  content is not an EPW (missing LOCATION header)")
            continue

        target.write_bytes(epw_bytes)
        log.info("saved EPW to %s (%d bytes)", target, len(epw_bytes))
        return True

    return False


# --------------------------------------------------------------------------------------
# Manual instructions
# --------------------------------------------------------------------------------------


def _prefix(dry_run: bool) -> str:
    return "[dry-run] would " if dry_run else ""


def manual_idf_instructions(target: Path) -> str:
    return (
        "IDF - DOE prototype small office (ASHRAE 90.1, 5 zone)\n"
        "  Option A (recommended): copy from your EnergyPlus install:\n"
        "    $ENERGYPLUS_DIR/ExampleFiles/RefBldgSmallOfficeNew2004_*.idf\n"
        "  Option B: download the prototype for your climate zone from\n"
        "    https://www.energycodes.gov/prototype-building-models  (Small Office)\n"
        f"  Save it as: {target}\n"
    )


def manual_epw_instructions(target: Path) -> str:
    return (
        "EPW - hot-humid Indian TMY (Chennai, WMO 432790, or equivalent)\n"
        "  Download from climate.onebuilding.org:\n"
        "    Region 2 (Asia) -> IND_India -> TN_Tamil_Nadu -> Chennai/Madras Intl AP (432790)\n"
        "    https://climate.onebuilding.org/WMO_Region_2_Asia/IND_India/\n"
        "  Unzip and extract the .epw file.\n"
        f"  Save it as: {target}\n"
    )


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def fetch_all(settings: Settings | None = None, *, dry_run: bool = False) -> int:
    """Fetch both assets. Returns 0 if both are present afterwards, 2 otherwise."""
    settings = settings or Settings.from_env()
    settings.simulation_dir.mkdir(parents=True, exist_ok=True)

    idf_ok = fetch_idf(settings.idf_path, dry_run=dry_run)
    epw_ok = fetch_epw(settings.epw_path, dry_run=dry_run)

    missing = []
    if not idf_ok:
        missing.append(manual_idf_instructions(settings.idf_path))
    if not epw_ok:
        missing.append(manual_epw_instructions(settings.epw_path))

    if missing:
        print("\n" + "=" * 72)
        print("Some assets need a manual download:\n")
        print("\n".join(missing))
        print("=" * 72)
        return 2

    print(f"assets ready:\n  {settings.idf_path}\n  {settings.epw_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the baseline IDF and EPW.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without touching the network or disk")
    args = parser.parse_args(argv)
    return fetch_all(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
