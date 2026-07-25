"""Snapshot versioning: dedupe, manifest, and diffs.

The materialiser is stubbed, so these cover everything *except* the eppy write - which is
deliberate, because the eppy write is the boring part and the dedupe is where a bug would
quietly ruin deliverable #2 (either by emitting hundreds of identical IDFs, or by dropping a
version that genuinely differed).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from common.models import AppliedControlState, SnapshotManifest
from simulation.snapshots import MANIFEST_NAME, SnapshotWriter, load_manifest, summarize

BASE = datetime(2017, 7, 15, 0, 0)


def state(
    cooling: float = 24.0,
    *,
    minutes: int = 0,
    trigger: str = "plan_commit",
    plan_id: str | None = "p1",
    patches: list[str] | None = None,
) -> AppliedControlState:
    return AppliedControlState(
        sim_time=BASE + timedelta(minutes=minutes),
        schedule_values={"CLGSETP_SCH": cooling, "HTGSETP_SCH": 21.0},
        applied_patches=patches or [],
        plan_id=plan_id,
        trigger=trigger,
    )


def stub_materializer(written: list[Path]):
    def materialize(base_idf: Path, control_state: AppliedControlState, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            f"! stub for {base_idf}\n{sorted(control_state.schedule_values.items())}\n",
            encoding="utf-8",
        )
        written.append(out_path)

    return materialize


@pytest.fixture
def writer(tmp_path: Path):
    written: list[Path] = []
    instance = SnapshotWriter(
        base_idf=tmp_path / "agentic.idf",
        versions_dir=tmp_path / "versions",
        materializer=stub_materializer(written),
    )
    return instance, written


# --------------------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------------------


def test_hash_ignores_time_and_trigger() -> None:
    """Same setpoints at a different moment is the same model, not a new version."""
    a = state(24.0, minutes=0, trigger="plan_commit")
    b = state(24.0, minutes=600, trigger="fallback")
    assert a.content_hash() == b.content_hash()


def test_hash_tracks_setpoint_changes() -> None:
    assert state(24.0).content_hash() != state(26.0).content_hash()


def test_hash_is_insensitive_to_float_noise() -> None:
    """Representation drift must not manufacture a version."""
    a = state(24.0)
    b = state(24.000000001)
    assert a.content_hash() == b.content_hash()


def test_hash_tracks_patches() -> None:
    assert state(24.0).content_hash() != state(24.0, patches=["abc"]).content_hash()


# --------------------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------------------


def test_first_commit_writes_v1(writer) -> None:
    instance, written = writer
    path = instance.commit(state(24.0))

    assert path is not None
    assert path.name.startswith("v1_")
    assert path.is_file()
    assert len(written) == 1


def test_identical_consecutive_state_writes_nothing(writer) -> None:
    instance, written = writer
    instance.commit(state(24.0, minutes=0))

    assert instance.commit(state(24.0, minutes=10)) is None
    assert instance.commit(state(24.0, minutes=20)) is None
    assert len(written) == 1
    assert instance.version_count == 1


def test_changed_state_writes_a_new_version(writer) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    second = instance.commit(state(26.0, minutes=840))

    assert second is not None
    assert second.name.startswith("v2_")
    assert instance.version_count == 2


def test_returning_to_a_previous_value_is_a_new_version(writer) -> None:
    """Dedupe is against the *head*, not against all history - v3 is a real model change."""
    instance, _ = writer
    instance.commit(state(24.0))
    instance.commit(state(26.0, minutes=840))
    third = instance.commit(state(24.0, minutes=960))

    assert third is not None
    assert instance.version_count == 3


def test_filename_carries_the_sim_date(writer) -> None:
    instance, _ = writer
    path = instance.commit(state(24.0, minutes=14 * 60))
    assert "20170715_1400" in path.name


# --------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------


def test_manifest_records_every_version(writer, tmp_path: Path) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    instance.commit(state(26.0, minutes=840))

    manifest = load_manifest(tmp_path / "versions")
    assert [e.version for e in manifest.entries] == [1, 2]
    assert manifest.entries[0].trigger == "plan_commit"
    assert manifest.entries[1].plan_id == "p1"
    assert (tmp_path / "versions" / MANIFEST_NAME).is_file()


def test_manifest_survives_a_new_writer(tmp_path: Path) -> None:
    """A restarted process must continue the series, not overwrite v1."""
    written: list[Path] = []
    first = SnapshotWriter(
        base_idf=tmp_path / "a.idf",
        versions_dir=tmp_path / "versions",
        materializer=stub_materializer(written),
    )
    first.commit(state(24.0))

    second = SnapshotWriter(
        base_idf=tmp_path / "a.idf",
        versions_dir=tmp_path / "versions",
        materializer=stub_materializer(written),
    )
    path = second.commit(state(26.0, minutes=840))

    assert path.name.startswith("v2_")
    assert second.version_count == 2


def test_manifest_entry_carries_the_state_for_later_diffing(writer) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    entry = instance.head
    assert entry.state.schedule_values["CLGSETP_SCH"] == pytest.approx(24.0)


def test_diff_summary_names_the_changed_schedule(writer) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    instance.commit(state(26.0, minutes=840))

    diff = instance.head.diff_summary
    assert any("CLGSETP_SCH" in line and "24.00 -> 26.00" in line for line in diff)


def test_first_diff_is_labelled_initial(writer) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    assert "initial" in instance.head.diff_summary[0]


def test_diff_reports_added_and_removed_schedules() -> None:
    before = AppliedControlState(sim_time=BASE, schedule_values={"A": 1.0})
    after = AppliedControlState(sim_time=BASE, schedule_values={"B": 2.0})
    diff = after.diff_against(before)
    assert "+B = 2.00" in diff
    assert "-A (was 1.00)" in diff


def test_diff_reports_patches() -> None:
    before = AppliedControlState(sim_time=BASE, schedule_values={})
    after = AppliedControlState(sim_time=BASE, schedule_values={}, applied_patches=["abc"])
    assert "+patch abc" in after.diff_against(before)


def test_paths_lists_the_series_in_order(writer) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    instance.commit(state(26.0, minutes=840))
    names = [p.name for p in instance.paths()]
    assert names[0].startswith("v1_")
    assert names[1].startswith("v2_")


def test_summarize_renders_the_series(writer, tmp_path: Path) -> None:
    instance, _ = writer
    instance.commit(state(24.0))
    text = summarize(tmp_path / "versions")
    assert "v1" in text
    assert "model series" in text


def test_summarize_on_empty_directory(tmp_path: Path) -> None:
    assert "no versions" in summarize(tmp_path / "nothing")


def test_missing_manifest_loads_as_empty(tmp_path: Path) -> None:
    manifest = SnapshotManifest.load(tmp_path / "absent.json")
    assert manifest.entries == []
    assert manifest.next_version == 1
