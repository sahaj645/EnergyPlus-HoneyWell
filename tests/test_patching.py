"""Patch application, validation, and byte-identical rollback.

``apply_operations`` is exercised against a fake IDF that mimics eppy's shape, so the operation
semantics are covered without EnergyPlus. Rollback is covered for real - it is a byte copy and
needs no eppy at all, which is precisely why it was designed that way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.models import (
    AppliedControlState,
    PatchOp,
    PatchOperation,
    PatchSpec,
    SnapshotEntry,
    SnapshotManifest,
)
from simulation.patching import MANIFEST_NAME, PatchError, apply_operations, rollback

# --------------------------------------------------------------------------------------
# A minimal eppy stand-in
# --------------------------------------------------------------------------------------


class FakeObject:
    def __init__(self, object_type: str, name: str, **fields) -> None:
        self._type = object_type
        self.Name = name
        self.fieldnames = ["Name", *fields.keys()]
        for key, value in fields.items():
            setattr(self, key, value)


class FakeIdf:
    def __init__(self) -> None:
        self.idfobjects: dict[str, list[FakeObject]] = {
            "SCHEDULE:CONSTANT": [
                FakeObject("SCHEDULE:CONSTANT", "CLGSETP_SCH", Hourly_Value=24.0),
            ],
            "TIMESTEP": [FakeObject("TIMESTEP", "", Number_of_Timesteps_per_Hour=6)],
        }

    def newidfobject(self, object_type: str, **fields) -> FakeObject:
        name = fields.pop("Name", "")
        obj = FakeObject(object_type, name, **fields)
        self.idfobjects.setdefault(object_type.upper(), []).append(obj)
        return obj

    def removeidfobject(self, obj: FakeObject) -> None:
        for objects in self.idfobjects.values():
            if obj in objects:
                objects.remove(obj)
                return


def spec_with(*operations: PatchOperation, reason: str = "test") -> PatchSpec:
    return PatchSpec(patch_id="testpatch", reason=reason, operations=list(operations))


# --------------------------------------------------------------------------------------
# PatchOperation validation
# --------------------------------------------------------------------------------------


def test_set_field_requires_a_field() -> None:
    with pytest.raises(ValueError, match="field"):
        PatchOperation(op=PatchOp.SET_FIELD, object_type="X", object_name="y", value=1)


def test_set_field_requires_a_value() -> None:
    with pytest.raises(ValueError, match="value"):
        PatchOperation(op=PatchOp.SET_FIELD, object_type="X", object_name="y", field="F")


def test_add_object_requires_fields() -> None:
    with pytest.raises(ValueError, match="fields"):
        PatchOperation(op=PatchOp.ADD_OBJECT, object_type="X", object_name="y")


def test_remove_object_needs_nothing_extra() -> None:
    operation = PatchOperation(op=PatchOp.REMOVE_OBJECT, object_type="X", object_name="y")
    assert "remove" in operation.describe()


def test_spec_requires_at_least_one_operation() -> None:
    with pytest.raises(ValueError):
        PatchSpec(operations=[])


# --------------------------------------------------------------------------------------
# apply_operations
# --------------------------------------------------------------------------------------


def test_set_field_updates_the_object() -> None:
    idf = FakeIdf()
    applied = apply_operations(
        idf,
        spec_with(
            PatchOperation(
                op=PatchOp.SET_FIELD,
                object_type="SCHEDULE:CONSTANT",
                object_name="CLGSETP_SCH",
                field="Hourly_Value",
                value=27.0,
            )
        ),
    )
    assert idf.idfobjects["SCHEDULE:CONSTANT"][0].Hourly_Value == 27.0
    assert applied == ["set SCHEDULE:CONSTANT[CLGSETP_SCH].Hourly_Value = 27.0"]


def test_set_field_on_a_missing_object_raises() -> None:
    with pytest.raises(PatchError, match="not found"):
        apply_operations(
            FakeIdf(),
            spec_with(
                PatchOperation(
                    op=PatchOp.SET_FIELD,
                    object_type="SCHEDULE:CONSTANT",
                    object_name="NOPE",
                    field="Hourly_Value",
                    value=27.0,
                )
            ),
        )


def test_set_field_on_a_missing_field_raises() -> None:
    """Typo'd field names must fail loudly, not create a stray attribute."""
    with pytest.raises(PatchError, match="no field"):
        apply_operations(
            FakeIdf(),
            spec_with(
                PatchOperation(
                    op=PatchOp.SET_FIELD,
                    object_type="SCHEDULE:CONSTANT",
                    object_name="CLGSETP_SCH",
                    field="Hourly_Valu",
                    value=27.0,
                )
            ),
        )


def test_add_object_appends() -> None:
    idf = FakeIdf()
    apply_operations(
        idf,
        spec_with(
            PatchOperation(
                op=PatchOp.ADD_OBJECT,
                object_type="SCHEDULE:CONSTANT",
                object_name="NEW_SCH",
                fields={"Hourly_Value": 22.0},
            )
        ),
    )
    names = [o.Name for o in idf.idfobjects["SCHEDULE:CONSTANT"]]
    assert "NEW_SCH" in names


def test_add_object_refuses_to_shadow_an_existing_one() -> None:
    with pytest.raises(PatchError, match="already exists"):
        apply_operations(
            FakeIdf(),
            spec_with(
                PatchOperation(
                    op=PatchOp.ADD_OBJECT,
                    object_type="SCHEDULE:CONSTANT",
                    object_name="CLGSETP_SCH",
                    fields={"Hourly_Value": 1.0},
                )
            ),
        )


def test_remove_object_deletes() -> None:
    idf = FakeIdf()
    apply_operations(
        idf,
        spec_with(
            PatchOperation(
                op=PatchOp.REMOVE_OBJECT,
                object_type="SCHEDULE:CONSTANT",
                object_name="CLGSETP_SCH",
            )
        ),
    )
    assert idf.idfobjects["SCHEDULE:CONSTANT"] == []


def test_operations_apply_in_order() -> None:
    idf = FakeIdf()
    apply_operations(
        idf,
        spec_with(
            PatchOperation(
                op=PatchOp.ADD_OBJECT,
                object_type="SCHEDULE:CONSTANT",
                object_name="TMP",
                fields={"Hourly_Value": 1.0},
            ),
            PatchOperation(
                op=PatchOp.SET_FIELD,
                object_type="SCHEDULE:CONSTANT",
                object_name="TMP",
                field="Hourly_Value",
                value=9.0,
            ),
        ),
    )
    tmp = [o for o in idf.idfobjects["SCHEDULE:CONSTANT"] if o.Name == "TMP"][0]
    assert tmp.Hourly_Value == 9.0


def test_object_lookup_is_case_insensitive() -> None:
    idf = FakeIdf()
    apply_operations(
        idf,
        spec_with(
            PatchOperation(
                op=PatchOp.SET_FIELD,
                object_type="schedule:constant",
                object_name="clgsetp_sch",
                field="Hourly_Value",
                value=25.0,
            )
        ),
    )
    assert idf.idfobjects["SCHEDULE:CONSTANT"][0].Hourly_Value == 25.0


# --------------------------------------------------------------------------------------
# Rollback (no eppy needed - it is a byte copy, by design)
# --------------------------------------------------------------------------------------


def seed_versions(versions_dir: Path, contents: list[str]) -> SnapshotManifest:
    """Write N version files plus a manifest describing them."""
    versions_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, text in enumerate(contents, start=1):
        filename = f"v{index}_seed.idf"
        (versions_dir / filename).write_text(text, encoding="utf-8")
        state = AppliedControlState(
            sim_time=f"2017-07-15T0{index}:00:00",
            schedule_values={"CLGSETP_SCH": 24.0 + index},
        )
        entries.append(
            SnapshotEntry(
                version=index,
                path=filename,
                content_hash=state.content_hash(),
                sim_time=state.sim_time,
                trigger="seed",
                state=state,
            )
        )
    manifest = SnapshotManifest(base_idf="agentic.idf", entries=entries)
    manifest.save(versions_dir / MANIFEST_NAME)
    return manifest


def test_rollback_is_byte_identical(tmp_path: Path) -> None:
    """The acceptance criterion: patch -> rollback round-trips byte-for-byte."""
    versions = tmp_path / "versions"
    original = "Schedule:Constant,\n  CLGSETP_SCH,\n  Temperature,\n  24.0;\n"
    patched = "Schedule:Constant,\n  CLGSETP_SCH,\n  Temperature,\n  27.0;\n"
    seed_versions(versions, [original, patched])

    new_path = rollback(1, versions_dir=versions)

    assert new_path.read_bytes() == (versions / "v1_seed.idf").read_bytes()
    assert new_path.read_bytes() != (versions / "v2_seed.idf").read_bytes()


def test_rollback_appends_rather_than_deleting(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    seed_versions(versions, ["a\n", "b\n"])

    rollback(1, versions_dir=versions)

    manifest = SnapshotManifest.load(versions / MANIFEST_NAME)
    assert [e.version for e in manifest.entries] == [1, 2, 3]
    assert (versions / "v1_seed.idf").is_file(), "history must stay intact"
    assert manifest.entries[-1].trigger == "rollback_to_v1"


def test_rollback_preserves_the_restored_control_state(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    seed_versions(versions, ["a\n", "b\n"])

    rollback(1, versions_dir=versions)

    manifest = SnapshotManifest.load(versions / MANIFEST_NAME)
    head, original = manifest.entries[-1], manifest.entries[0]
    assert head.state.schedule_values == original.state.schedule_values
    assert head.content_hash == original.content_hash


def test_rollback_to_unknown_version_raises(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    seed_versions(versions, ["a\n"])
    with pytest.raises(PatchError, match="not in manifest"):
        rollback(99, versions_dir=versions)


def test_rollback_detects_a_missing_file(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    seed_versions(versions, ["a\n"])
    (versions / "v1_seed.idf").unlink()
    with pytest.raises(PatchError, match="missing"):
        rollback(1, versions_dir=versions)


def test_rollback_then_rollback_still_matches_the_original(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    seed_versions(versions, ["original\n", "patched\n"])
    # Compare against the bytes actually on disk, not the Python literal: the seeding helper
    # goes through write_text, which newline-translates on Windows.
    expected = (versions / "v1_seed.idf").read_bytes()

    first = rollback(1, versions_dir=versions)
    second = rollback(1, versions_dir=versions)

    assert first.read_bytes() == second.read_bytes() == expected


def test_manifest_json_is_readable(tmp_path: Path) -> None:
    """A human has to be able to read this - it is the deliverable's index."""
    versions = tmp_path / "versions"
    seed_versions(versions, ["a\n"])
    payload = json.loads((versions / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert payload["entries"][0]["version"] == 1
    assert "content_hash" in payload["entries"][0]
