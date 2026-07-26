"""L2 self-correction: feed the guardian's own reasons back to the planner.

The executor journals a :class:`~common.models.GuardianEvent` (decision + verbatim reasons) for
every *new* plan commit, buffered in memory (rule R1 - no DB I/O on the callback thread) until
the harness drains it to SQLite after the run. :class:`FeedbackTracker` watches that same
in-memory buffer from the planning side, one snapshot per cycle:

* the moment a plan comes back anything but cleanly ``accepted``, its reasons become the next
  cycle's ``PREVIOUS PLAN FEEDBACK`` (:mod:`agent.digest`'s slot, empty since Session 5), and
* the plan that acts on that feedback records ``corrects_plan_id`` pointing back at the plan
  being corrected, so the journal can render the clipped -> corrected chain.

One instance is shared between the digest-building closure (``observe`` + ``pending_feedback``,
called every LLM cycle) and :class:`agent.scheduler.Scheduler` (``pending_plan_id`` captured
alongside the digest, ``resolve`` called once the correction has been committed). Pure
bookkeeping - no I/O, no clock reads - so it is trivially unit-testable with a list of
hand-built ``GuardianEvent``s.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from common.models import GuardianDecision, GuardianEvent


@dataclass
class FeedbackTracker:
    """Latches the most recent non-accepted guardian verdict pending a correction.

    ``observe`` is idempotent-safe to call with a growing list every cycle (it only looks at
    events past the last one it has already seen). Only one correction is tracked at a time - a
    second bad verdict before the first is resolved simply replaces it as "the thing to fix
    next", which matches there being only one plan in flight at a time (rule: one worker).
    """

    _seen: int = field(default=0, repr=False)
    _plan_id: str | None = field(default=None, repr=False)
    _reasons: tuple[str, ...] = field(default=(), repr=False)

    def observe(self, events: list[GuardianEvent]) -> None:
        """Scan any events past the last call's high-water mark; latch the newest non-accepted."""
        for event in events[self._seen :]:
            if event.decision is not GuardianDecision.ACCEPTED:
                self._plan_id = event.plan_id
                self._reasons = tuple(_split_reasons(event.note))
        self._seen = len(events)

    def pending_feedback(self) -> list[str] | None:
        """Reasons for the digest's ``PREVIOUS PLAN FEEDBACK`` section, or ``None`` if clean."""
        return list(self._reasons) if self._reasons else None

    def pending_plan_id(self) -> str | None:
        """The plan being corrected, for the next plan's ``corrects_plan_id`` - or ``None``."""
        return self._plan_id

    def resolve(self) -> None:
        """Call once a correction has been committed, so it is not fed again next cycle."""
        self._plan_id = None
        self._reasons = ()


def _split_reasons(note: str) -> list[str]:
    """``GuardianEvent.note`` is ``"; "``-joined reasons (see ``guardian.executor._journal``)."""
    return [part.strip() for part in note.split(";") if part.strip()]


__all__ = ["FeedbackTracker"]
