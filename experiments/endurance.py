"""Endurance runner: >=1 simulated month of the full agent loop, resumable.

EnergyPlus has no notion of pausing a running simulation and reattaching later with its
internal state intact - there is no checkpoint/restore in the runtime API this project uses.
So "resumable" is built the only way that is actually achievable: the month is split into
day-sized (configurable via ``--chunk-days``) **chunks**, each chunk a completely separate
EnergyPlus invocation of :func:`experiments.ab.run_agent_arm` - the same building block the
A/B harness's ``agent`` arm is made of - over a RunPeriod scoped to just that chunk. Progress
is written to a small JSON checkpoint after every chunk, so a kill (Ctrl-C, OOM, a reboot)
loses at most one chunk's work, and ``--resume`` continues from the next uncompleted one
rather than restarting the whole month.

**What carries across chunks and what does not**, deliberately:

* The :class:`~agent.cache.PlanCache` is a **single object reused for the whole process
  lifetime** - cache hit rate keeps accumulating realistically across days, which is most of
  the point of running long. It re-warms from empty after a restart (a resumed run's cache is
  cold); that is a safe degraded state, not a correctness issue - a cold cache just means a few
  more planner calls than an uninterrupted run would have made.
* The :class:`~agent.events.DriftEventDetector` is **rebuilt fresh every chunk** (via
  ``run_agent_arm``'s own default when ``events`` is not supplied). Its severe-error tracking
  keys off a specific ``.err`` file's mtime and running count; carrying it across chunks - each
  with its *own* ``.err`` file - would make an old chunk's error count shadow a new chunk's
  real one. A fresh detector per chunk is the safe choice, at the cost of the peak/drift history
  resetting daily, which is a fine trade for a run whose job is to prove endurance, not to
  squeeze out the last percent of cache efficiency.

**Exceptions are counted, never swallowed.** The EnergyPlus callback already can't crash the
sim (rule R1) and the scheduler's worker already logs+counts its own failures - those are
already-contained, expected noise. What this module additionally guards is its own outer
per-chunk driving loop: if a chunk's ``run_agent_arm`` call raises (a fatal EnergyPlus error, a
missing asset, anything unexpected), the exception is logged with its full traceback, counted
in the checkpoint's ``unhandled_exceptions``, the checkpoint is saved (without advancing past
the failed chunk, so ``--resume`` retries it), and then **the exception is re-raised** - the
run stops loudly rather than silently skipping a broken day.
"""

from __future__ import annotations

import argparse
import json
import os
import traceback
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import PreparedModel
from experiments.ab import run_agent_arm
from simulation.run_baseline import RunPeriodSpec, hottest_week

log = get_logger("experiments.endurance")

RESULTS_ROOT = "experiments/results"
CHECKPOINT_NAME = "checkpoint.json"


# --------------------------------------------------------------------------------------
# Checkpoint
# --------------------------------------------------------------------------------------


@dataclass
class Cumulative:
    """Counters accumulated across every completed chunk. Never reset by a resume."""

    timesteps: int = 0
    planner_calls: int = 0
    cache_hits: int = 0
    holds: int = 0
    events: int = 0
    clipped_steps: int = 0
    guardian_fallbacks: int = 0
    watchdog_trips: int = 0
    scheduler_failed: int = 0
    unhandled_exceptions: int = 0

    def add_chunk(self, result) -> None:
        self.timesteps += result.timesteps
        self.planner_calls += result.plans_made
        self.cache_hits += result.cache_hits
        self.holds += result.holds
        self.events += result.events
        self.clipped_steps += result.clipped_steps
        self.guardian_fallbacks += result.fallback_steps
        self.watchdog_trips += result.watchdog_trips
        self.scheduler_failed += result.scheduler_failed

    def summary(self) -> str:
        return (
            f"timesteps={self.timesteps} planner_calls={self.planner_calls} "
            f"cache_hits={self.cache_hits} holds={self.holds} events={self.events} "
            f"clipped={self.clipped_steps} guardian_fallbacks={self.guardian_fallbacks} "
            f"watchdog_trips={self.watchdog_trips} scheduler_failed={self.scheduler_failed} "
            f"unhandled_exceptions={self.unhandled_exceptions}"
        )


@dataclass
class Checkpoint:
    label: str
    start: str  # MM-DD
    days_total: int
    chunk_days: int
    next_chunk_index: int = 0
    cumulative: Cumulative = field(default_factory=Cumulative)
    run_id: str = ""
    updated_at: str = ""

    @property
    def chunks_total(self) -> int:
        return -(-self.days_total // self.chunk_days)  # ceil division

    @property
    def done(self) -> bool:
        return self.next_chunk_index >= self.chunks_total

    def to_dict(self) -> dict:
        payload = asdict(self)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> Checkpoint:
        cumulative = Cumulative(**payload.pop("cumulative"))
        return cls(cumulative=cumulative, **payload)

    def save(self, path: Path) -> None:
        """Atomic write: a kill mid-write leaves the previous checkpoint intact, never a
        half-written, unreadable one."""
        self.updated_at = datetime.now().isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> Checkpoint:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# Chunk scheduling
# --------------------------------------------------------------------------------------


def chunk_spec(start: date, chunk_index: int, chunk_days: int, days_total: int) -> RunPeriodSpec:
    """RunPeriod for one chunk: ``chunk_days`` long, clipped so the run never exceeds
    ``days_total``.
    """
    offset = chunk_index * chunk_days
    span = min(chunk_days, days_total - offset)
    if span <= 0:
        raise ValueError(f"chunk_index {chunk_index} is past the end of a {days_total}-day run")
    chunk_start = start + timedelta(days=offset)
    chunk_end = chunk_start + timedelta(days=span - 1)
    return RunPeriodSpec(
        label=f"endurance_c{chunk_index:03d}",
        begin_month=chunk_start.month, begin_day=chunk_start.day,
        end_month=chunk_end.month, end_day=chunk_end.day,
    )


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def _parse_start(value: str) -> date:
    month, day = (int(part) for part in value.split("-", 1))
    return date(2017, month, day)


def run_endurance(
    settings: Settings | None = None,
    *,
    label: str = "default",
    days: int = 30,
    chunk_days: int = 1,
    start: date | None = None,
    timesteps_per_hour: int = 6,
    timeout_s: float = 30.0,
    resume: bool = False,
    results_root: Path | None = None,
) -> Checkpoint:
    """Run (or resume) an endurance run. Returns the final checkpoint.

    Background-friendly by construction: no interactive prompts, every chunk's progress is
    logged and checkpointed, and the whole thing is one synchronous call suitable for
    ``nohup python -m experiments.endurance --days 30 &``.
    """
    from agent.cache import PlanCache
    from experiments.kpis import load_carbon, load_tariff

    settings = settings or Settings.from_env()
    install_dir = eplus_path.require_energyplus()

    for path, hint in (
        (settings.idf_path, "fetch_assets"),
        (settings.epw_path, "fetch_assets"),
        (settings.simulation_dir / "agentic.idf", "prepare_idf"),
        (settings.simulation_dir / "agentic_model.json", "prepare_idf"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{path} not found. Run `python -m simulation.{hint}` first.")

    if start is None:
        anchor = hottest_week(settings.epw_path)
        start = date(2017, anchor.begin_month, anchor.begin_day)

    results_root = results_root or (settings.repo_root / RESULTS_ROOT / f"endurance_{label}")
    checkpoint_path = results_root / CHECKPOINT_NAME

    if checkpoint_path.is_file():
        if not resume:
            raise FileExistsError(
                f"{checkpoint_path} already exists. Pass --resume to continue it, or choose a "
                "different --label to start a new run."
            )
        checkpoint = Checkpoint.load(checkpoint_path)
        mismatched = (
            checkpoint.days_total != days
            or checkpoint.chunk_days != chunk_days
            or checkpoint.start != f"{start:%m-%d}"
        )
        if mismatched:
            raise ValueError(
                "--resume requested with different parameters than the checkpoint recorded "
                f"(checkpoint: days={checkpoint.days_total} chunk_days={checkpoint.chunk_days} "
                f"start={checkpoint.start}); rerun with matching flags or pick a new --label."
            )
        log.info(
            "resuming %s from chunk %d/%d (%s)", label, checkpoint.next_chunk_index,
            checkpoint.chunks_total, checkpoint.cumulative.summary(),
        )
    else:
        if resume:
            raise FileNotFoundError(f"--resume given but no checkpoint at {checkpoint_path}")
        checkpoint = Checkpoint(
            label=label, start=f"{start:%m-%d}", days_total=days, chunk_days=chunk_days,
            run_id=f"endurance-{label}-{datetime.now():%Y%m%dT%H%M%S}",
        )
        checkpoint.save(checkpoint_path)
        log.info("starting endurance run %r: %d day(s) in %d-day chunks", label, days, chunk_days)

    model = PreparedModel.load(settings.simulation_dir / "agentic_model.json")
    tariff = load_tariff(settings.data_dir / "tariff.csv")
    carbon = load_carbon(settings.data_dir / "carbon_intensity.csv")
    cache = PlanCache(tariff=tariff, carbon=carbon)  # carried across chunks; see module docstring

    while not checkpoint.done:
        index = checkpoint.next_chunk_index
        spec = chunk_spec(start, index, chunk_days, days)
        chunk_dir = results_root / f"chunk_{index:03d}"
        chunk_run_id = f"{checkpoint.run_id}-c{index:03d}"

        log.info(
            "chunk %d/%d: %s (%02d/%02d -> %02d/%02d)", index + 1, checkpoint.chunks_total,
            spec.label, spec.begin_month, spec.begin_day, spec.end_month, spec.end_day,
        )
        print(f"[{index + 1}/{checkpoint.chunks_total}] running {spec.label} -> {chunk_dir}")

        try:
            result = run_agent_arm(
                model=model, settings=settings, spec=spec, timesteps_per_hour=timesteps_per_hour,
                out_dir=chunk_dir, run_id=chunk_run_id, install_dir=install_dir,
                timeout_s=timeout_s, cache=cache, events=None,
            )
        except Exception:
            checkpoint.cumulative.unhandled_exceptions += 1
            log.exception("chunk %d failed - counted, not swallowed; run stops here", index)
            checkpoint.save(checkpoint_path)
            print(f"chunk {index} raised; checkpoint saved at {checkpoint_path}.")
            print(traceback.format_exc())
            raise

        checkpoint.cumulative.add_chunk(result)
        checkpoint.next_chunk_index += 1
        checkpoint.save(checkpoint_path)
        cache.save(results_root / "cache_stats.json")

        log.info(
            "chunk %d/%d done: %s",
            index + 1, checkpoint.chunks_total, checkpoint.cumulative.summary(),
        )
        print(f"  done: {checkpoint.cumulative.summary()}")

    log.info("endurance run %r complete: %s", label, checkpoint.cumulative.summary())
    return checkpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=">=1 simulated month of the full agent loop, resumable, background-friendly."
    )
    parser.add_argument("--days", type=int, default=30, help="total simulated days (default 30)")
    parser.add_argument(
        "--chunk-days", type=int, default=1, help="days per resumable chunk (default 1)"
    )
    parser.add_argument(
        "--start", type=_parse_start, default=None, help="MM-DD; default: hottest week's start"
    )
    parser.add_argument("--timestep", type=int, default=6, dest="timesteps_per_hour")
    parser.add_argument("--timeout", type=float, default=30.0, help="planner timeout, seconds")
    parser.add_argument("--label", default="default", help="names the results/checkpoint dir")
    parser.add_argument(
        "--resume", action="store_true", help="continue a previous run with this --label"
    )
    parser.add_argument("--out", type=Path, default=None, help="override the results directory")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    checkpoint = run_endurance(
        settings, label=args.label, days=args.days, chunk_days=args.chunk_days, start=args.start,
        timesteps_per_hour=args.timesteps_per_hour, timeout_s=args.timeout, resume=args.resume,
        results_root=args.out,
    )
    print(f"\nendurance run {args.label!r} complete: {checkpoint.cumulative.summary()}")
    return 0


__all__ = ["Checkpoint", "Cumulative", "chunk_spec", "run_endurance"]


if __name__ == "__main__":
    raise SystemExit(main())
