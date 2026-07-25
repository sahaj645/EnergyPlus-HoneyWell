"""A thread-safe single-slot holder for the latest committed plan.

This is the one place the planner thread and the control thread meet. The planner runs on its
own cadence and **commits** a plan here; the executor, on the callback thread, **reads** the
latest one. Exactly one plan is ever held - a newer commit replaces the previous outright,
because a stale plan is worse than useless and there is never a reason to actuate an old one
when a newer one exists.

Why a dedicated object rather than a bare variable behind a lock: the read side runs inside the
EnergyPlus callback (rule R1), so `get()` must be non-blocking and must never raise. A plain
attribute risks a torn read of the (plan, timestamp, generation) tuple if the planner is
mid-commit; the lock makes each read see one internally-consistent snapshot.

`generation` increments on every commit, so a reader can cheaply tell "is this the same plan I
already filtered?" without comparing plan contents - which the executor uses to avoid
re-journalling an unchanged plan every timestep.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from common.models import Plan


@dataclass(frozen=True)
class SlotContents:
    """An internally-consistent snapshot of the slot."""

    plan: Plan
    committed_at: datetime | None
    generation: int


class PlanSlot:
    """Holds exactly the latest committed :class:`~common.models.Plan`. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plan: Plan | None = None
        self._committed_at: datetime | None = None
        self._generation = 0

    def commit(self, plan: Plan, *, at: datetime | None = None) -> int:
        """Replace the held plan. Returns the new generation number.

        ``at`` is the *simulation* time of the commit when known (the watchdog measures freshness
        in sim time, not wall-clock). Omit it and freshness tracking is left to the caller.
        """
        with self._lock:
            self._plan = plan
            self._committed_at = at
            self._generation += 1
            return self._generation

    def get(self) -> Plan | None:
        """Return the latest committed plan, or ``None`` if nothing has been committed.

        Non-blocking in practice (the lock is only ever held for a few field assignments) and
        never raises - safe to call from the callback.
        """
        with self._lock:
            return self._plan

    def snapshot(self) -> SlotContents | None:
        """Return plan + commit time + generation as one consistent snapshot, or ``None``."""
        with self._lock:
            if self._plan is None:
                return None
            return SlotContents(
                plan=self._plan,
                committed_at=self._committed_at,
                generation=self._generation,
            )

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def clear(self) -> None:
        """Drop the held plan. The executor then falls back until a fresh plan is committed."""
        with self._lock:
            self._plan = None
            self._committed_at = None
            self._generation += 1


__all__ = ["PlanSlot", "SlotContents"]
