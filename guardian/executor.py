"""The executor: turn the latest committed plan into safe actuation, every timestep.

This is the guardian's hand on the actuator. Each timestep it:

1. reads the latest plan from the thread-safe :class:`~common.planslot.PlanSlot`,
2. *holds* it - resolves the setpoint in force right now for each (zone, actuator),
3. runs each zone through :meth:`guardian.core.Guardian.filter` (whitelist, envelope, rate),
4. assembles the survivors into an :class:`~common.models.ApprovedPlan`, and
5. calls ``control.write_setpoints`` - the one function that touches an actuator.

If there is no plan, or the watchdog says the planner has gone quiet, or the guardian rejects
everything, it writes the **baseline** instead. So the executor runs the building indefinitely
with no planner at all - the planner only ever *improves* on a safe default.

**Rule R1.** ``step`` runs inside the EnergyPlus callback, so it does no blocking I/O. Guardian
events and the per-timestep verdict stream are buffered in memory and drained by the harness
*after* the run (:attr:`guardian_events`, :attr:`verdict_log`). Telemetry is the store's
already-batched concern.

**Rule R2.** The executor never constructs an :class:`~common.models.ApprovedPlan` itself; it
asks the guardian (:meth:`guardian.core.Guardian.approve`). The only actuator write takes that
guardian-built object.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.log import get_logger
from common.models import (
    Actuator,
    ApprovedPlan,
    BuildingState,
    ControlInterface,
    GuardianDecision,
    GuardianEvent,
    GuardianVerdict,
    PlanStep,
    PreparedModel,
    SetpointPlan,
)
from common.planslot import PlanSlot
from guardian.core import Guardian, RateHistory
from guardian.fallback import baseline_plan
from guardian.watchdog import Watchdog

log = get_logger("guardian.executor")

_TEMPERATURE_SETPOINTS = (Actuator.COOLING_SETPOINT_C, Actuator.HEATING_SETPOINT_C)

#: Stable plan id for everything the executor emits. Steps are always offset-0 "apply now"
#: values, so the bus anchors this once and applies every timestep - no unbounded growth of
#: per-plan anchors over a year-long run.
EXECUTOR_PLAN_ID = "guardian-exec"


@dataclass
class ExecutorStats:
    steps: int = 0
    fallback_steps: int = 0
    clipped_steps: int = 0
    watchdog_trips: int = 0

    def summary(self) -> str:
        return (
            f"steps={self.steps} fallback={self.fallback_steps} "
            f"clipped={self.clipped_steps} watchdog_trips={self.watchdog_trips}"
        )


@dataclass
class VerdictRecord:
    """One timestep's worth of guardian reasons, for the post-run stream."""

    at: datetime
    source: str  # "plan" or "fallback"
    reasons: list[str] = field(default_factory=list)


class Executor:
    """Drives the actuator from the committed plan through the guardian, every timestep."""

    def __init__(
        self,
        *,
        guardian: Guardian,
        model: PreparedModel,
        plan_slot: PlanSlot,
        plan_interval: timedelta,
        control: ControlInterface | None = None,
        watchdog: Watchdog | None = None,
        run_id: str = "",
    ) -> None:
        self.guardian = guardian
        self.model = model
        self.plan_slot = plan_slot
        self.control = control
        self.watchdog = watchdog or Watchdog(plan_interval=plan_interval)
        self.run_id = run_id

        self.rate_history = RateHistory.empty()
        self.stats = ExecutorStats()
        #: Buffered for the harness to persist after the run (rule R1). Also read live, via
        #: :meth:`events_snapshot`, by the L2 feedback loop on the planner worker thread - hence
        #: the lock, even though the callback-side appends stay cheap (list.append under a lock
        #: it never contends for, since planning cycles are hourly at most).
        self._events_lock = threading.Lock()
        self.guardian_events: list[GuardianEvent] = []
        #: Buffered verdict stream for printing after the run.
        self.verdict_log: list[VerdictRecord] = []

        self._anchor: dict[str, datetime] = {}
        self._last_journaled_generation = -1

    # -- the per-timestep entry point --------------------------------------------------

    def provide(self, now: datetime) -> ApprovedPlan | None:
        """Bus ``plan_provider`` hook: compute and apply this timestep's safe setpoints.

        Returns ``None`` - the write happens here, via ``control.write_setpoints``, so the bus
        must not write again. (The executor *is* the thing that calls the actuator, per the
        session's design; returning the plan for the bus to apply would be the other valid
        wiring, but then two code paths could disagree about what was written.)
        """
        if self.control is None:
            raise RuntimeError("Executor.control is not wired to a ControlInterface")

        state = self.control.read_state()
        if state is None:
            return None  # warmup / not observable yet

        approved, record = self.decide(now, state)
        self.control.write_setpoints(approved, now=now)

        self.stats.steps += 1
        if approved.fallback:
            self.stats.fallback_steps += 1
        if record.reasons:
            self.verdict_log.append(record)
        return None

    # -- the pure-ish decision (no actuation, no I/O) ----------------------------------

    def decide(self, now: datetime, state: BuildingState) -> tuple[ApprovedPlan, VerdictRecord]:
        """Choose the ApprovedPlan for ``now`` given ``state``. Actuation is the caller's job.

        Kept separate from :meth:`provide` so the decision can be exercised without a bus.
        """
        watchdog_event = self.watchdog.check(now)
        if watchdog_event is not None:
            with self._events_lock:
                self.guardian_events.append(watchdog_event)
            self.stats.watchdog_trips += 1
            log.warning("watchdog tripped at %s; forcing baseline", now)

        snapshot = self.plan_slot.snapshot()
        if snapshot is None or self.watchdog.tripped:
            reason = "no_plan" if snapshot is None else "watchdog"
            approved = baseline_plan(self.model, now, reason=reason)
            self._record_baseline_rate(now, approved)
            return approved, VerdictRecord(at=now, source="fallback", reasons=[])

        plan = snapshot.plan
        anchor = self._anchor.setdefault(plan.plan_id, now)
        in_force = self._hold(plan, anchor=anchor, now=now)

        verdicts = [
            self.guardian.filter(in_force, zone_state, self.rate_history)
            for zone_state in state.zones
        ]
        approved = self.guardian.approve(verdicts, plan_id=EXECUTOR_PLAN_ID, now=now)

        self._record_applied_rate(now, approved)

        reasons = [reason for verdict in verdicts for reason in verdict.reasons]
        if reasons:
            self.stats.clipped_steps += 1

        # Freshness + journal are per *commit*, not per timestep: a plan that just sits in the
        # slot is not "fresh". Reset the watchdog and journal only when a new plan appears.
        if snapshot.generation != self._last_journaled_generation:
            self._last_journaled_generation = snapshot.generation
            if approved.decision is not GuardianDecision.REJECTED:
                self.watchdog.note_valid_plan(now)
            with self._events_lock:
                self.guardian_events.append(self._journal(plan, approved, verdicts, now))

        return approved, VerdictRecord(at=now, source="plan", reasons=reasons)

    # -- helpers -----------------------------------------------------------------------

    def _hold(self, plan: SetpointPlan, *, anchor: datetime, now: datetime) -> SetpointPlan:
        """Resolve the in-force value per (zone, actuator): latest step whose offset has elapsed.

        Setpoints are step schedules, so "interpolate/hold" is a hold - the most recent step
        that has come due wins; steps still in the future do not act yet. Emitted at offset 0.
        """
        elapsed_minutes = (now - anchor).total_seconds() / 60.0
        chosen: dict[tuple[str, Actuator], PlanStep] = {}
        for step in plan.steps:
            if step.offset_minutes > elapsed_minutes:
                continue
            key = (step.zone, step.actuator)
            current = chosen.get(key)
            if current is None or step.offset_minutes >= current.offset_minutes:
                chosen[key] = step
        held = [step.model_copy(update={"offset_minutes": 0}) for step in chosen.values()]
        return plan.model_copy(update={"steps": held})

    def _record_applied_rate(self, now: datetime, approved: ApprovedPlan) -> None:
        for step in approved.steps:
            if step.actuator in _TEMPERATURE_SETPOINTS:
                self.rate_history = self.rate_history.record(step.zone, now, step.value)

    def _record_baseline_rate(self, now: datetime, approved: ApprovedPlan) -> None:
        # Keep the rate anchor continuous across a fallback so a returning plan cannot leap.
        self._record_applied_rate(now, approved)

    def _journal(
        self,
        plan: SetpointPlan,
        approved: ApprovedPlan,
        verdicts: list[GuardianVerdict],
        now: datetime,
    ) -> GuardianEvent:
        reasons = [reason for verdict in verdicts for reason in verdict.reasons]
        note = "; ".join(reasons[:12]) if reasons else "accepted, no changes"
        return GuardianEvent(
            at=now,
            plan_id=plan.plan_id,
            decision=approved.decision,
            note=note[:1000],
        )

    # -- live read side (planner worker thread, during the run) ------------------------

    def events_snapshot(self) -> list[GuardianEvent]:
        """Copy of the guardian events journaled so far - the L2 feedback loop's read side.

        Safe to call from the planner worker while the callback thread keeps appending: it is
        the same lock the append side takes, so this always sees a consistent, if possibly
        slightly stale, prefix.
        """
        with self._events_lock:
            return list(self.guardian_events)

    # -- persistence (called by the harness AFTER the run - never on the callback) -----

    def drain_events(self, store, *, run_id: str | None = None) -> int:
        """Write buffered guardian events to the store. Returns the number written."""
        target_run = run_id or self.run_id or None
        with self._events_lock:
            pending, self.guardian_events = self.guardian_events, []
        for event in pending:
            store.write_guardian_event(event, run_id=target_run)
        return len(pending)


__all__ = ["EXECUTOR_PLAN_ID", "Executor", "ExecutorStats", "VerdictRecord"]
