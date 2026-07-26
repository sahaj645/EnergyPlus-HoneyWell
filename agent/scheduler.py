"""The scheduler: run the planner off the callback thread, feed the guardian's plan slot.

The EnergyPlus callback must never wait on the LLM (rule R1). So planning happens on a worker
thread: the callback only ever calls :meth:`Scheduler.on_timestep`, which is cheap - it decides
whether a cycle is due and, if so, *starts* a thread and returns immediately. The worker builds
the digest, calls the planner (seconds of inference), lowers the result to the guardian's
representation, and commits it to the :class:`~common.planslot.PlanSlot`. The executor picks it
up on some later timestep. Nothing blocks.

Triggers: **startup** (first timestep) and **hourly** (each new simulation hour). An
**event** trigger is left as an interface (:class:`EventDetector`) for Session 6; the default
detector never fires.

Two safety properties:

* **Hard timeout.** A worker that outruns ``timeout_s`` (wall-clock inference budget) may be
  preempted by the next trigger. Only one cycle is in flight at a time; a new trigger while one
  is still running is dropped unless the running one has exceeded the timeout.
* **Late results discarded.** Every trigger bumps an epoch. A worker only commits if its epoch
  is still current when it finishes - so a slow plan that lands after a newer trigger fired is
  thrown away rather than actuating stale intent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from agent.planner import Planner
from common.log import get_logger
from common.models import BuildingState, TriggerEnum
from common.planslot import PlanSlot
from common.store import TelemetryStore

log = get_logger("agent.scheduler")

#: ``(now) -> digest string``. Built by the harness (reads state + forecast).
DigestProvider = Callable[[datetime], str]


@runtime_checkable
class EventDetector(Protocol):
    """Session 6 hook: decide whether an out-of-band event warrants an immediate replan."""

    def should_trigger(self, now: datetime, state: BuildingState | None) -> bool: ...


class NullEventDetector:
    """Default detector: never fires. Replaced in Session 6."""

    def should_trigger(self, now: datetime, state: BuildingState | None) -> bool:
        return False


@dataclass
class SchedulerStats:
    triggers: int = 0
    plans_made: int = 0  # planner returned a valid Plan
    accepted: int = 0  # committed to the slot
    failed: int = 0  # planner returned None (unreachable / unrepairable)
    timeouts: int = 0  # preempted for exceeding the wall-clock budget
    discarded: int = 0  # completed but a newer trigger had superseded it

    def summary(self) -> str:
        return (
            f"triggers={self.triggers} plans_made={self.plans_made} accepted={self.accepted} "
            f"failed={self.failed} timeouts={self.timeouts} discarded={self.discarded}"
        )


class Scheduler:
    """Drives planning cycles from simulation-time triggers, entirely off the callback thread."""

    def __init__(
        self,
        *,
        planner: Planner,
        plan_slot: PlanSlot,
        digest_provider: DigestProvider,
        baseline: dict[tuple[str, str], float] | None = None,
        timeout_s: float = 30.0,
        store: TelemetryStore | None = None,
        run_id: str = "",
        event_detector: EventDetector | None = None,
    ) -> None:
        self.planner = planner
        self.plan_slot = plan_slot
        self.digest_provider = digest_provider
        self.baseline = baseline
        self.timeout_s = timeout_s
        self.store = store
        self.run_id = run_id
        self.event_detector = event_detector or NullEventDetector()

        self.stats = SchedulerStats()
        self._lock = threading.Lock()
        self._epoch = 0
        self._inflight_epoch: int | None = None
        self._inflight_started = 0.0
        self._started = False
        self._last_hour: datetime | None = None
        self._worker: threading.Thread | None = None

    # -- callback-thread entry point (cheap, non-blocking) -----------------------------

    def on_timestep(self, now: datetime, state: BuildingState | None = None) -> None:
        """Called every timestep from the callback. Starts a worker if a cycle is due.

        Must not block: it only reads a couple of fields and maybe spawns a daemon thread.
        """
        trigger = self._due(now, state)
        if trigger is None:
            return
        with self._lock:
            if not self._can_start(now):
                return
            self._epoch += 1
            epoch = self._epoch
            self._inflight_epoch = epoch
            self._inflight_started = time.monotonic()
            self.stats.triggers += 1
        self._last_hour = now.replace(minute=0, second=0, microsecond=0)
        self._started = True

        worker = threading.Thread(
            target=self._run_cycle, args=(now, trigger, epoch), name=f"planner-{epoch}", daemon=True
        )
        self._worker = worker
        worker.start()

    def _due(self, now: datetime, state: BuildingState | None) -> TriggerEnum | None:
        if not self._started:
            return TriggerEnum.STARTUP
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        if self._last_hour is None or this_hour > self._last_hour:
            return TriggerEnum.HOURLY
        if self.event_detector.should_trigger(now, state):
            return TriggerEnum.EVENT
        return None

    def _can_start(self, now: datetime) -> bool:
        """True if no worker is in flight, or the in-flight one has blown its time budget."""
        if self._inflight_epoch is None:
            return True
        elapsed = time.monotonic() - self._inflight_started
        if elapsed > self.timeout_s:
            # Preempt: the old worker's epoch is now stale, so whatever it returns is discarded.
            self.stats.timeouts += 1
            log.warning("planner cycle exceeded %.0fs budget; preempting", self.timeout_s)
            return True
        return False

    # -- worker thread -----------------------------------------------------------------

    def _run_cycle(self, now: datetime, trigger: TriggerEnum, epoch: int) -> None:
        """Build digest -> plan -> lower -> commit. Runs off the callback thread."""
        try:
            digest = self.digest_provider(now)
            plan = self.planner.plan(digest, now=now, trigger=trigger)

            if plan is None:
                with self._lock:
                    self.stats.failed += 1
                log.info("planner returned no plan (trigger=%s)", trigger)
                return

            with self._lock:
                self.stats.plans_made += 1
                if epoch != self._epoch:
                    self.stats.discarded += 1
                    log.info("plan from epoch %d discarded; epoch is now %d", epoch, self._epoch)
                    return

            setpoints = plan.to_setpoint_plan(now=now, baseline=self.baseline)
            if self.store is not None:
                self.store.write_plan(plan, run_id=self.run_id)
            # The plan slot is the guardian-governed boundary: the executor filters every
            # committed plan through the guardian on every timestep (rule R2). Committing here
            # hands the candidate to that authoritative pass, it does not bypass it.
            self.plan_slot.commit(setpoints, at=now)

            with self._lock:
                self.stats.accepted += 1
            log.info(
                "committed plan %s (%d action(s), ecms=%s)",
                plan.plan_id,
                len(plan.actions),
                [e.value for e in plan.ecms],
            )
        except Exception:
            with self._lock:
                self.stats.failed += 1
            log.exception("planning cycle failed")
        finally:
            with self._lock:
                if self._inflight_epoch == epoch:
                    self._inflight_epoch = None

    # -- shutdown ----------------------------------------------------------------------

    def join(self, timeout: float | None = None) -> None:
        """Wait for the current worker to finish - used by harnesses at run end."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)


__all__ = [
    "DigestProvider",
    "EventDetector",
    "NullEventDetector",
    "Scheduler",
    "SchedulerStats",
]
