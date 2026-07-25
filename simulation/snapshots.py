"""Versioned IDF snapshots - competition deliverable #2.

Every time a plan is committed, the control state actually in force is materialised into
``simulation/versions/v{N}_{simdate}.idf``. Together with ``baseline.idf`` that series *is* the
deliverable: "here is the model we started from, and here is every model the agent produced".

Three properties worth stating, because each one is a decision rather than an accident:

**Deduped by content, not by event.** A plan that re-commits the same setpoints - which happens
constantly, since most planning cycles change nothing - writes no file. The hash covers the
control-relevant content only (see :meth:`~common.models.AppliedControlState.content_hash`), so
the version series tracks *distinct models*, not planner activity. Without this a day-long run
would emit hundreds of byte-identical IDFs and the deliverable would be noise.

**The manifest is the index, and it carries the state.** Each entry stores the full
:class:`~common.models.AppliedControlState` it was built from, so a diff between any two
versions is computable after the fact without re-parsing IDFs.

**Materialisation is injectable.** The default writer uses eppy and therefore needs EnergyPlus.
Everything else here - versioning, dedupe, manifest, diffs - is pure and is exercised in tests
with a stub materialiser, because that logic is where the subtle bugs live.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from common.log import get_logger
from common.models import AppliedControlState, SnapshotEntry, SnapshotManifest

log = get_logger("simulation.snapshots")

MANIFEST_NAME = "manifest.json"

#: ``(base_idf, state, out_path) -> None``. Writes the materialised model to ``out_path``.
Materializer = Callable[[Path, AppliedControlState, Path], None]


def eppy_materializer(install_dir: Path | str | None = None) -> Materializer:
    """Default materialiser: load the base IDF, stamp in the schedule values, save.

    Only ``Schedule:Constant`` values are written. That is not a limitation in practice -
    ``prepare_idf`` has already rewritten every thermostat setpoint schedule into a
    ``Schedule:Constant``, so the constant values *are* the control state. Structural changes
    are the job of :mod:`simulation.patching`, which produces its own versions.
    """

    def materialize(base_idf: Path, state: AppliedControlState, out_path: Path) -> None:
        from simulation.idf_io import load_idf, save_idf

        idf = load_idf(base_idf, install_dir)
        by_name = {
            str(schedule.Name).upper(): schedule
            for schedule in idf.idfobjects.get("SCHEDULE:CONSTANT", [])
        }
        missing = []
        for name, value in state.schedule_values.items():
            schedule = by_name.get(name.upper())
            if schedule is None:
                missing.append(name)
                continue
            schedule.Hourly_Value = float(value)
        if missing:
            log.warning("snapshot: %d schedule(s) not in base IDF: %s", len(missing), missing)
        save_idf(idf, out_path)

    return materialize


class SnapshotWriter:
    """Materialises committed control states into a versioned IDF series."""

    def __init__(
        self,
        *,
        base_idf: Path | str,
        versions_dir: Path | str,
        materializer: Materializer | None = None,
        install_dir: Path | str | None = None,
    ) -> None:
        self.base_idf = Path(base_idf)
        self.versions_dir = Path(versions_dir)
        self.manifest_path = self.versions_dir / MANIFEST_NAME
        self._materialize = materializer or eppy_materializer(install_dir)
        self.manifest = SnapshotManifest.load(self.manifest_path)
        if not self.manifest.base_idf:
            self.manifest = self.manifest.model_copy(update={"base_idf": str(self.base_idf)})

    # -- introspection -----------------------------------------------------------------

    @property
    def version_count(self) -> int:
        return len(self.manifest.entries)

    @property
    def head(self) -> SnapshotEntry | None:
        return self.manifest.head

    def paths(self) -> list[Path]:
        """Every materialised IDF, oldest first."""
        return [self.versions_dir / entry.path for entry in self.manifest.entries]

    # -- the operation that matters ----------------------------------------------------

    def commit(self, applied_control_state: AppliedControlState) -> Path | None:
        """Materialise ``applied_control_state`` as the next version.

        Returns the path written, or **``None`` when the commit was deduped** - the state was
        control-identical to the current head, so no file and no manifest entry were produced.
        Returning the previous path instead would be worse: callers could not distinguish "a new
        model exists" from "nothing changed", which is exactly what the dedupe is measuring.
        """
        content_hash = applied_control_state.content_hash()
        head = self.manifest.head

        if head is not None and head.content_hash == content_hash:
            log.debug(
                "snapshot deduped at %s (hash %s unchanged since v%d)",
                applied_control_state.sim_time,
                content_hash,
                head.version,
            )
            return None

        version = self.manifest.next_version
        filename = f"v{version}_{applied_control_state.sim_time:%Y%m%d_%H%M}.idf"
        out_path = self.versions_dir / filename
        self.versions_dir.mkdir(parents=True, exist_ok=True)

        self._materialize(self.base_idf, applied_control_state, out_path)
        if not out_path.is_file():
            raise RuntimeError(f"materializer did not produce {out_path}")

        entry = SnapshotEntry(
            version=version,
            path=filename,
            content_hash=content_hash,
            sim_time=applied_control_state.sim_time,
            trigger=applied_control_state.trigger,
            plan_id=applied_control_state.plan_id,
            diff_summary=applied_control_state.diff_against(head.state if head else None),
            state=applied_control_state,
        )
        self.manifest = self.manifest.model_copy(
            update={"entries": [*self.manifest.entries, entry]}
        )
        self.manifest.save(self.manifest_path)

        log.info(
            "snapshot v%d -> %s (%s)", version, filename, "; ".join(entry.diff_summary[:3])
        )
        return out_path

    def record_external(self, entry: SnapshotEntry) -> SnapshotEntry:
        """Append an entry produced elsewhere - used by :mod:`simulation.patching`.

        Snapshots and patches share one version series on purpose: the deliverable is a single
        ordered history of the model, not two interleaved ones a reader has to reconcile.
        """
        self.manifest = self.manifest.model_copy(
            update={"entries": [*self.manifest.entries, entry]}
        )
        self.manifest.save(self.manifest_path)
        return entry


def load_manifest(versions_dir: Path | str) -> SnapshotManifest:
    """Read the manifest for a versions directory (empty if none yet)."""
    return SnapshotManifest.load(Path(versions_dir) / MANIFEST_NAME)


def summarize(versions_dir: Path | str) -> str:
    """Render the version series as a table - the deliverable, at a glance."""
    manifest = load_manifest(versions_dir)
    if not manifest.entries:
        return f"no versions in {versions_dir}"

    lines = [f"model series in {versions_dir} (base: {manifest.base_idf})"]
    for entry in manifest.entries:
        lines.append(
            f"  v{entry.version:<3} {entry.sim_time:%Y-%m-%d %H:%M}  "
            f"{entry.trigger:<14} {entry.content_hash}  {'; '.join(entry.diff_summary[:2])}"
        )
    return "\n".join(lines)


__all__ = [
    "MANIFEST_NAME",
    "Materializer",
    "SnapshotWriter",
    "eppy_materializer",
    "load_manifest",
    "summarize",
]
