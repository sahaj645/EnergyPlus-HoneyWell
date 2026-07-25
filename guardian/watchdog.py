"""Watchdog: force the fallback when the planner goes quiet.

The rule is deliberately simple and measured in **simulation** time: if no fresh, valid plan
has been seen for more than two planning intervals, the planner is presumed dead and the
executor holds the baseline. A run that finishes on baseline is far more useful than one that
stalls waiting for a plan that will never come.

"Valid" means a committed plan the guardian did not wholly reject - a plan that was clipped to
safety still counts as the planner being alive. The executor reports those via
:meth:`note_valid_plan`; every timestep it also calls :meth:`check`, which returns a
:class:`~common.models.GuardianEvent` **once** on the transition into staleness (and once again
on recovery). The event is persisted by the caller after the run, not written here - this class
performs no I/O, holds no LLM or network dependency, and never blocks the callback.

Freshness clock: it starts at the first :meth:`check`, so a run that never gets a plan trips
exactly once, two intervals in, rather than being either silent forever or noisy from step one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from common.models import GuardianDecision, GuardianEvent, Violation, ViolationCode


class Watchdog:
    """Tracks plan freshness in simulation time and signals when to force the fallback."""

    def __init__(self, *, plan_interval: timedelta, max_intervals: int = 2) -> None:
        if plan_interval <= timedelta(0):
            raise ValueError("plan_interval must be positive")
        if max_intervals < 1:
            raise ValueError("max_intervals must be >= 1")
        self.plan_interval = plan_interval
        self.max_intervals = max_intervals
        self._last_valid_at: datetime | None = None
        self._tripped = False

    @property
    def tripped(self) -> bool:
        """True while the fallback is being forced (no fresh valid plan)."""
        return self._tripped

    @property
    def deadline_after(self) -> timedelta:
        return self.plan_interval * self.max_intervals

    def note_valid_plan(self, now: datetime) -> None:
        """Record that a usable plan was seen at simulation time ``now``."""
        self._last_valid_at = now
        self._tripped = False

    def check(self, now: datetime) -> GuardianEvent | None:
        """Evaluate freshness at ``now``. Returns an event only on a state transition.

        - Into staleness: a ``WATCHDOG_TIMEOUT`` event (the caller forces the fallback and
          persists the row).
        - Back to fresh is handled by :meth:`note_valid_plan`, which clears the trip; no event
          is emitted on recovery here to keep the journal to one row per stale episode.
        """
        if self._last_valid_at is None:
            # Start the clock on first sight; no plan yet is not itself a fault.
            self._last_valid_at = now
            return None

        stale = now - self._last_valid_at > self.deadline_after
        if stale and not self._tripped:
            self._tripped = True
            age = now - self._last_valid_at
            return GuardianEvent(
                at=now,
                plan_id="watchdog",
                decision=GuardianDecision.REJECTED,
                violations=[
                    Violation(
                        code=ViolationCode.WATCHDOG_TIMEOUT,
                        message=(
                            f"no fresh valid plan for {age} "
                            f"(> {self.max_intervals} x {self.plan_interval}); forcing fallback"
                        ),
                    )
                ],
                note="watchdog forced baseline fallback",
            )
        return None


__all__ = ["Watchdog"]
