"""Versioned, rollback-safe IDF editing - the ``patch_model`` primitive.

This is the backbone of the MCP ``patch_model`` tool and, later, L3 self-heal: the agent reads
``eplusout.err``, notices it has broken something, and patches the model. That is a powerful and
genuinely dangerous capability, so the primitive is built defensively from the start:

**Validate before accepting.** The patched model is written to a temporary file and re-parsed
before it is admitted to the version series. A patch that produces an unparseable IDF is
rejected and the head is untouched - the agent cannot brick the model by emitting nonsense.

**Never edit in place.** Every patch produces a *new* version. ``baseline.idf`` and every prior
version stay byte-identical forever, which is what makes rollback trivially correct.

**Rollback is a byte copy, not a re-derivation.** ``rollback(v)`` copies the bytes of version
``v`` forward as a new version. Re-applying the inverse of a patch would be clever and wrong -
floating-point round-trips and eppy's field normalisation mean a "reversed" patch is not
guaranteed to reproduce the original file. Copying bytes is guaranteed.

The blast radius is still real and worth naming: this can change any object in the model,
including ones the guardian knows nothing about. The guardian reviews *plans*, not patches. Any
autonomous use of this needs its own review gate - which does not exist yet.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from common.log import get_logger
from common.models import (
    AppliedControlState,
    PatchOp,
    PatchSpec,
    SnapshotEntry,
    SnapshotManifest,
)
from simulation.snapshots import MANIFEST_NAME, SnapshotWriter

log = get_logger("simulation.patching")


class PatchError(RuntimeError):
    """A patch could not be applied."""


class PatchValidationError(PatchError):
    """The patched model did not parse; the patch was rejected and nothing changed."""


# --------------------------------------------------------------------------------------
# Operation application (works against any eppy-shaped object; unit-tested with a fake)
# --------------------------------------------------------------------------------------


def _find(idf, object_type: str, object_name: str):
    for candidate in idf.idfobjects.get(object_type.upper(), []):
        if str(getattr(candidate, "Name", "")).upper() == object_name.upper():
            return candidate
    return None


def apply_operations(idf, spec: PatchSpec) -> list[str]:
    """Apply every operation in ``spec`` to ``idf`` in place. Returns what was done.

    Raises :class:`PatchError` on the first operation that cannot be applied, leaving ``idf``
    partially modified - which is fine, because the caller works on a throwaway parse and only
    the validated result is ever written to the version series.
    """
    applied: list[str] = []

    for index, operation in enumerate(spec.operations):
        target = _find(idf, operation.object_type, operation.object_name)

        if operation.op is PatchOp.SET_FIELD:
            if target is None:
                raise PatchError(
                    f"operation {index}: {operation.object_type}[{operation.object_name}] "
                    "not found"
                )
            if operation.field not in target.fieldnames:
                raise PatchError(
                    f"operation {index}: {operation.object_type} has no field "
                    f"{operation.field!r}"
                )
            setattr(target, operation.field, operation.value)

        elif operation.op is PatchOp.ADD_OBJECT:
            if target is not None:
                raise PatchError(
                    f"operation {index}: {operation.object_type}[{operation.object_name}] "
                    "already exists"
                )
            idf.newidfobject(
                operation.object_type.upper(),
                Name=operation.object_name,
                **operation.fields,
            )

        elif operation.op is PatchOp.REMOVE_OBJECT:
            if target is None:
                raise PatchError(
                    f"operation {index}: {operation.object_type}[{operation.object_name}] "
                    "not found"
                )
            idf.removeidfobject(target)

        applied.append(operation.describe())

    return applied


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def validate_idf(path: Path | str, install_dir: Path | str | None = None) -> None:
    """Re-parse ``path``; raise :class:`PatchValidationError` if it does not load."""
    from simulation.idf_io import load_idf

    try:
        load_idf(path, install_dir)
    except Exception as exc:  # eppy raises a zoo of exception types
        raise PatchValidationError(f"patched model at {path} does not parse: {exc}") from exc


# --------------------------------------------------------------------------------------
# The public primitives
# --------------------------------------------------------------------------------------


def apply_patch(
    idf_path: Path | str,
    patch_spec: PatchSpec,
    *,
    versions_dir: Path | str,
    install_dir: Path | str | None = None,
    validate: bool = True,
) -> Path:
    """Apply ``patch_spec`` to ``idf_path`` and return the new version's path.

    The source model is never modified. On any failure - bad operation, unparseable result -
    nothing is added to the version series and the exception propagates.
    """
    from simulation.idf_io import load_idf, save_idf

    source = Path(idf_path)
    versions = Path(versions_dir)
    versions.mkdir(parents=True, exist_ok=True)

    idf = load_idf(source, install_dir)
    applied = apply_operations(idf, patch_spec)

    with tempfile.TemporaryDirectory() as scratch:
        staged = Path(scratch) / "patched.idf"
        save_idf(idf, staged)
        if validate:
            validate_idf(staged, install_dir)

        manifest = SnapshotManifest.load(versions / MANIFEST_NAME)
        version = manifest.next_version
        filename = f"v{version}_patch_{patch_spec.patch_id}.idf"
        destination = versions / filename
        shutil.copyfile(staged, destination)

    _append_entry(
        versions,
        version=version,
        filename=filename,
        trigger="patch",
        state=_patch_state(patch_spec, manifest),
        diff_summary=applied,
        base_idf=str(source),
    )
    log.info("patch %s -> v%d (%s)", patch_spec.patch_id, version, filename)
    return destination


def rollback(
    to_version: int,
    *,
    versions_dir: Path | str,
) -> Path:
    """Restore version ``to_version`` as a new head. Returns the new version's path.

    The new file is a **byte-for-byte copy** of the target, which is what makes
    patch -> rollback a true round trip. History is append-only: rolling back adds a version,
    it never deletes one.
    """
    versions = Path(versions_dir)
    manifest = SnapshotManifest.load(versions / MANIFEST_NAME)

    target = manifest.entry(to_version)
    if target is None:
        available = [entry.version for entry in manifest.entries]
        raise PatchError(f"version {to_version} not in manifest; have {available}")

    source = versions / target.path
    if not source.is_file():
        raise PatchError(f"version {to_version} is in the manifest but {source} is missing")

    version = manifest.next_version
    filename = f"v{version}_rollback_to_v{to_version}.idf"
    destination = versions / filename
    shutil.copyfile(source, destination)

    _append_entry(
        versions,
        version=version,
        filename=filename,
        trigger=f"rollback_to_v{to_version}",
        state=target.state,
        diff_summary=[f"restored v{to_version} ({target.content_hash}) byte-for-byte"],
        base_idf=manifest.base_idf,
    )
    log.info("rolled back to v%d -> v%d (%s)", to_version, version, filename)
    return destination


# --------------------------------------------------------------------------------------
# Manifest plumbing
# --------------------------------------------------------------------------------------


def _patch_state(spec: PatchSpec, manifest: SnapshotManifest) -> AppliedControlState:
    """Carry the head's control state forward, recording the new patch id.

    A structural patch does not by itself change setpoints, so the schedule values are
    inherited; what changes is the patch list, which is part of the content hash.
    """
    head = manifest.head
    previous = head.state if head else None
    return AppliedControlState(
        sim_time=spec.created_at,
        schedule_values=dict(previous.schedule_values) if previous else {},
        applied_patches=[*(previous.applied_patches if previous else []), spec.patch_id],
        plan_id=previous.plan_id if previous else None,
        trigger="patch",
    )


def _append_entry(
    versions_dir: Path,
    *,
    version: int,
    filename: str,
    trigger: str,
    state: AppliedControlState,
    diff_summary: list[str],
    base_idf: str,
) -> SnapshotEntry:
    manifest = SnapshotManifest.load(versions_dir / MANIFEST_NAME)
    entry = SnapshotEntry(
        version=version,
        path=filename,
        content_hash=state.content_hash(),
        sim_time=state.sim_time,
        trigger=trigger,
        plan_id=state.plan_id,
        diff_summary=diff_summary,
        state=state,
    )
    updated = manifest.model_copy(
        update={
            "entries": [*manifest.entries, entry],
            "base_idf": manifest.base_idf or base_idf,
        }
    )
    updated.save(versions_dir / MANIFEST_NAME)
    return entry


def writer_for(versions_dir: Path | str, base_idf: Path | str, **kwargs) -> SnapshotWriter:
    """Convenience: a :class:`SnapshotWriter` over the same version series."""
    return SnapshotWriter(base_idf=base_idf, versions_dir=versions_dir, **kwargs)


__all__ = [
    "PatchError",
    "PatchValidationError",
    "apply_operations",
    "apply_patch",
    "rollback",
    "validate_idf",
    "writer_for",
]
