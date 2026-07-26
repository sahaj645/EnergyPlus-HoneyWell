"""L3: scripted, re-runnable planted-fault self-heal demo.

The full loop, exercised end to end with the *real* Session 6/9 machinery, not a mock:

1. **``plant_fault``** corrupts a known schedule reference in a copy of ``agentic.idf`` - it
   removes the ``Schedule:Constant`` a zone's thermostat points at, via the same three-verb
   :mod:`simulation.patching` primitive every other patch in this repo goes through (so the
   fault itself is just version 2 of an ordinary, append-only history: v1 clean, v2 faulted).
2. One **receding-horizon chunk** (:func:`experiments.ab.run_bare` - one EnergyPlus start-up,
   no live callback, exactly the H6 contingency's unit of work) runs the faulted model and
   EnergyPlus logs Severe errors: the dangling schedule reference cannot be resolved.
3. The **real** :class:`agent.events.DriftEventDetector` watches that chunk's ``eplusout.err``
   and fires its ``severe_error`` trigger - the same code path a live run's reactive scheduler
   uses, not a demo-only check.
4. :class:`agent.repair.RepairPlanner` is handed a **repair digest** (the filtered error log +
   the model's known schedules) and returns a candidate :class:`~common.models.PatchSpec`.
5. :func:`simulation.patching.apply_patch` applies it - **validated before accepted** - as
   version 3. The chunk re-runs; if it is still not clean, or the patch did not even parse,
   :func:`simulation.patching.rollback` returns the series to the last known-good version (v1)
   as a new version, and the loop resumes there instead of on a still-broken model.

Every step is journaled (JSON, printed, and - for the one real LLM call - ``llm_calls`` via the
shared :class:`~common.store.TelemetryStore`). ``--replay`` skips the LLM call entirely: it reuses
the most recent *repair* patch already on disk under ``--versions-dir`` (tagged with a
``repair-`` patch id so it is unambiguous which manifest entries are repairs vs. the planted
fault), so the demo is deterministic and does not depend on Ollama being up.

Requires EnergyPlus; the live (non-``--replay``) path also needs a running Ollama with the model
pulled. Neither exists in CI - like every other harness here, this module is import-safe and
fails fast with a clear message only when its entry point actually runs.
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import (
    AppliedControlState,
    PatchOp,
    PatchOperation,
    PatchSpec,
    PreparedModel,
)
from common.store import TelemetryStore
from experiments.ab import prepare_arm_idf, run_bare
from simulation.patching import PatchValidationError, apply_patch, rollback
from simulation.run_baseline import RunPeriodSpec, count_errors, hottest_week
from simulation.snapshots import SnapshotWriter, load_manifest

log = get_logger("experiments.self_heal")

RESULTS_ROOT = "experiments/results"
DEFAULT_VERSIONS_DIR = "experiments/results/self_heal_versions"
REPAIR_PREFIX = "repair-"
FAULT_PREFIX = "fault-"


# --------------------------------------------------------------------------------------
# Journal: a plain list of steps, printed and persisted as JSON - no schema change needed
# --------------------------------------------------------------------------------------


@dataclass
class JournalEntry:
    at: str
    step: str
    detail: dict


class Journal:
    def __init__(self) -> None:
        self.entries: list[JournalEntry] = []

    def log(self, step: str, **detail: object) -> None:
        entry = JournalEntry(at=datetime.now(UTC).isoformat(), step=step, detail=detail)
        self.entries.append(entry)
        log.info("[%s] %s", step, detail)

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps([asdict(e) for e in self.entries], indent=2, default=str),
            encoding="utf-8",
        )


# --------------------------------------------------------------------------------------
# Step 1: plant the fault
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FaultRecord:
    zone: str
    schedule: str
    patch_id: str


def plant_fault(
    base_idf: Path, model: PreparedModel, *, versions_dir: Path, install_dir: Path
) -> tuple[FaultRecord, Path]:
    """Remove the first zone's cooling schedule - a dangling reference EnergyPlus cannot start.

    Goes through :func:`simulation.patching.apply_patch` like any other patch: syntactically
    valid (a schedule object may legally not exist), semantically broken (something else still
    references it by name), which is exactly the shape of fault ``patch_model``'s own docstring
    warns about - "the guardian reviews plans, not patches".
    """
    binding = next((z for z in model.zones if z.cooling_schedule), None)
    if binding is None or not binding.cooling_schedule:
        raise RuntimeError(
            "no zone with a cooling schedule to fault - is agentic_model.json stale?"
        )

    schedule_name = binding.cooling_schedule
    patch_id = f"{FAULT_PREFIX}{uuid.uuid4().hex[:8]}"
    spec = PatchSpec(
        patch_id=patch_id,
        reason=f"self_heal_demo: planted fault - dangling reference to {schedule_name}",
        operations=[
            PatchOperation(
                op=PatchOp.REMOVE_OBJECT,
                object_type="SCHEDULE:CONSTANT",
                object_name=schedule_name,
            )
        ],
    )
    faulted_path = apply_patch(base_idf, spec, versions_dir=versions_dir, install_dir=install_dir)
    return FaultRecord(zone=binding.zone, schedule=schedule_name, patch_id=patch_id), faulted_path


# --------------------------------------------------------------------------------------
# Step 3: diagnose - filter the error log down to what the repair planner needs to see
# --------------------------------------------------------------------------------------


def diagnose(err_path: Path, *, max_lines: int = 20) -> list[str]:
    """The last ``max_lines`` Severe/Fatal lines, verbatim - the repair digest's error log."""
    if not err_path.is_file():
        return []
    text = err_path.read_text(encoding="utf-8", errors="replace")
    flagged = [
        line.strip()
        for line in text.splitlines()
        if "** severe" in line.lower() or "**  fatal" in line.lower()
    ]
    return flagged[-max_lines:]


# --------------------------------------------------------------------------------------
# --replay: reuse the newest repair patch already on disk, no LLM call
# --------------------------------------------------------------------------------------


def _find_replay_patch(versions_dir: Path) -> Path:
    """Newest manifest entry whose applied patch is tagged ``repair-`` - a prior live run's fix."""
    manifest = load_manifest(versions_dir)
    for entry in reversed(manifest.entries):
        last_patch = entry.state.applied_patches[-1] if entry.state.applied_patches else ""
        if entry.trigger == "patch" and last_patch.startswith(REPAIR_PREFIX):
            path = versions_dir / entry.path
            if path.is_file():
                return path
    raise RuntimeError(
        f"--replay found no prior repair patch under {versions_dir}; "
        "run the demo once without --replay first"
    )


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


@dataclass
class SelfHealResult:
    mode: str  # "live" or "replay"
    fault: FaultRecord
    fault_severe: int
    fault_fatal: int
    event_fired: bool
    event_reason: str | None
    patch_id: str
    healed: bool
    rolled_back: bool
    journal_path: str
    out_dir: str

    def summary(self) -> str:
        return (
            f"mode={self.mode} fault_severe={self.fault_severe} event_fired={self.event_fired} "
            f"healed={self.healed} rolled_back={self.rolled_back}"
        )


def _one_day_spec(epw_path: Path) -> RunPeriodSpec:
    """A single day, anchored on the hottest week's first day - one receding-horizon chunk."""
    week = hottest_week(epw_path)
    return RunPeriodSpec(
        label="self_heal_1d", begin_month=week.begin_month, begin_day=week.begin_day,
        end_month=week.begin_month, end_day=week.begin_day,
    )


def run_self_heal(
    settings: Settings | None = None,
    *,
    replay: bool = False,
    out_dir: Path | None = None,
    versions_dir: Path | None = None,
    timeout_s: float = 30.0,
) -> SelfHealResult:
    """Run the full plant-fault -> detect -> repair -> resume loop once."""
    settings = settings or Settings.from_env()
    install_dir = eplus_path.require_energyplus()

    index_path = settings.simulation_dir / "agentic_model.json"
    if not index_path.is_file():
        raise FileNotFoundError(
            f"{index_path} not found. Run `python -m simulation.prepare_idf` first."
        )
    model = PreparedModel.load(index_path)

    out_dir = out_dir or (
        settings.repo_root / RESULTS_ROOT / f"self_heal_{datetime.now():%Y%m%dT%H%M%S}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    versions_dir = versions_dir or (settings.repo_root / DEFAULT_VERSIONS_DIR)
    versions_dir.mkdir(parents=True, exist_ok=True)

    journal = Journal()
    mode = "replay" if replay else "live"
    journal.log("start", mode=mode, out_dir=str(out_dir), versions_dir=str(versions_dir))

    spec = _one_day_spec(settings.epw_path)
    base_idf = out_dir / "base.idf"
    prepare_arm_idf(
        Path(model.idf_path), spec, timesteps_per_hour=6, out_path=base_idf,
        install_dir=install_dir,
    )

    # v1: register the clean baseline, so a failed repair has a known-good version to fall back
    # to - not just "one step back", which after a fault is still broken.
    writer = SnapshotWriter(base_idf=base_idf, versions_dir=versions_dir, install_dir=install_dir)
    clean_state = AppliedControlState(
        sim_time=datetime.now(UTC), schedule_values=dict(model.constant_schedules),
        trigger="self_heal_baseline",
    )
    writer.commit(clean_state)
    clean_version = writer.head.version if writer.head else 1
    journal.log("baseline_registered", version=clean_version)

    # v2: plant the fault.
    fault, faulted_idf = plant_fault(
        base_idf, model, versions_dir=versions_dir, install_dir=install_dir
    )
    journal.log("plant_fault", zone=fault.zone, schedule=fault.schedule, patch_id=fault.patch_id)

    chunk0_dir = out_dir / "chunk_fault"
    exit0 = run_bare(faulted_idf, settings.epw_path, chunk0_dir)
    severe0, fatal0 = count_errors(chunk0_dir / "eplusout.err")
    journal.log("chunk_run", label="fault", exit_code=exit0, severe=severe0, fatal=fatal0)
    if severe0 == 0 and fatal0 == 0:
        raise RuntimeError(
            "planted fault produced no Severe/Fatal error - plant_fault needs a stronger break"
        )

    # The real Session 6 reactive-trigger detector, watching this chunk's own error log.
    from agent.events import DriftEventDetector

    detector = DriftEventDetector(
        tariff_band_at=lambda _hour: "mid", carbon_band_at=lambda _hour: "mid",
        err_path=chunk0_dir / "eplusout.err",
    )
    event_fired = detector.should_trigger(datetime.now(UTC), None)
    journal.log("event_trigger", fired=event_fired, reason=detector.last_reason)
    if not event_fired:
        raise RuntimeError("severe_error event did not fire - check DriftEventDetector wiring")

    error_lines = diagnose(chunk0_dir / "eplusout.err")

    db_path = out_dir / "self_heal.sqlite"
    with TelemetryStore(db_path) as store:
        run_id = f"self-heal-{mode}-{datetime.now():%Y%m%dT%H%M%S}"
        store.start_run(run_id, label="self_heal_demo", notes=mode)

        if replay:
            patched_idf = _find_replay_patch(versions_dir)
            patch_id = "REPLAY"
            journal.log("repair_planned", mode="REPLAY", reused_path=str(patched_idf))
        else:
            from agent.repair import RepairPlanner, build_repair_digest

            digest = build_repair_digest(error_lines, model)
            planner = RepairPlanner(
                model=settings.ollama_model, host=settings.ollama_host, timeout_s=timeout_s,
                keep_alive=settings.ollama_keep_alive, store=store, run_id=run_id,
            )
            patch_spec = planner.plan(digest)
            if patch_spec is None:
                journal.log("repair_failed", reason="planner returned no patch")
                journal.save(out_dir / "journal.json")
                raise RuntimeError("RepairPlanner produced no patch - see llm_calls for why")

            # Tag it so --replay can find this exact patch later, unambiguously.
            patch_spec = patch_spec.model_copy(
                update={"patch_id": f"{REPAIR_PREFIX}{patch_spec.patch_id}"}
            )
            journal.log(
                "repair_planned", mode="LIVE", patch_id=patch_spec.patch_id,
                ops=patch_spec.describe(),
            )

            try:
                patched_idf = apply_patch(
                    faulted_idf, patch_spec, versions_dir=versions_dir, install_dir=install_dir,
                )
            except PatchValidationError as exc:
                journal.log("patch_rejected", error=str(exc))
                journal.save(out_dir / "journal.json")
                raise
            patch_id = patch_spec.patch_id

        journal.log("patch_applied", patch_id=patch_id, path=str(patched_idf))

        chunk1_dir = out_dir / "chunk_patched"
        exit1 = run_bare(patched_idf, settings.epw_path, chunk1_dir)
        severe1, fatal1 = count_errors(chunk1_dir / "eplusout.err")
        journal.log("chunk_run", label="patched", exit_code=exit1, severe=severe1, fatal=fatal1)

        healed = exit1 == 0 and fatal1 == 0 and severe1 == 0
        rolled_back = False
        if healed:
            journal.log("resumed", note="loop resumed cleanly on the patched model")
        else:
            rollback(clean_version, versions_dir=versions_dir)
            rolled_back = True
            journal.log(
                "rollback", to_version=clean_version,
                note="patch did not clear the error; resumed on the last known-good version",
            )

    journal_path = out_dir / "journal.json"
    journal.save(journal_path)

    result = SelfHealResult(
        mode=mode, fault=fault, fault_severe=severe0, fault_fatal=fatal0,
        event_fired=event_fired, event_reason=detector.last_reason, patch_id=patch_id,
        healed=healed, rolled_back=rolled_back, journal_path=str(journal_path),
        out_dir=str(out_dir),
    )
    log.info("self-heal demo (%s) finished: %s", mode, result.summary())
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="L3 self-heal demo: plant a fault, detect it, repair it, resume."
    )
    parser.add_argument(
        "--replay", action="store_true",
        help="reuse the last successful repair patch from --versions-dir; no LLM call",
    )
    parser.add_argument("--out", type=Path, default=None, help="per-run output directory")
    parser.add_argument(
        "--versions-dir", type=Path, default=None,
        help=f"persistent patch history (default: {DEFAULT_VERSIONS_DIR})",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="repair planner timeout, s")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    result = run_self_heal(
        settings, replay=args.replay, out_dir=args.out, versions_dir=args.versions_dir,
        timeout_s=args.timeout,
    )

    label = "REPLAY" if result.mode == "replay" else "LIVE"
    print(f"\n[{label}] self-heal demo -> {result.out_dir}")
    print(f"  fault    : zone={result.fault.zone} schedule={result.fault.schedule} "
          f"severe={result.fault_severe} fatal={result.fault_fatal}")
    print(f"  event    : fired={result.event_fired} reason={result.event_reason}")
    print(f"  patch    : {result.patch_id}")
    print(f"  outcome  : healed={result.healed} rolled_back={result.rolled_back}")
    print(f"  journal  : {result.journal_path}")
    ok = result.healed or result.rolled_back  # either outcome means the loop resumed safely
    outcome = "healed" if result.healed else "rolled back"
    print(f"[{'PASS' if ok else 'FAIL'}] loop resumed ({outcome})")
    return 0 if ok else 1


__all__ = [
    "FaultRecord",
    "Journal",
    "SelfHealResult",
    "diagnose",
    "plant_fault",
    "run_self_heal",
]


if __name__ == "__main__":
    raise SystemExit(main())
