"""Plan cache: reuse a past plan when the situation is discretely the same, no LLM call.

Local inference is not free - a few seconds per cycle - and most cycles recur: a hot occupied
midday under a solar-cheap tariff looks the same today as yesterday. This cache turns the
planning *situation* into a coarse key and, on a hit, replays the stored plan with its
timestamps shifted to now. The replayed plan is **still lowered and still guardian-filtered**
on every timestep by the executor - the cache skips the model, never the safety layer.

The key is deliberately coarse (`(hour band, occupancy bucket, 2 C outdoor bin, tariff band,
carbon band)`) so that near-identical situations collide onto one entry. Too fine a key never
hits; too coarse a key reuses a plan built for a different problem. This granularity matches the
levers the planner actually has.

Alongside the cache is the **hold pre-filter**: on an hourly tick with no event and nothing
drifting (comfort pressure below epsilon), the right move is to *do nothing* - keep the current
plan, make no call, log a hold. Doing nothing well is most of the savings.

`calls_made` / `calls_avoided` (and `holds`) are counters the harness persists, because "how
often did we actually hit the model?" is the number that justifies the cache existing.

This supersedes the `agent/plan_cache.py` scaffold.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from agent.digest import band, terciles
from common.log import get_logger
from common.models import BuildingState, Plan

log = get_logger("agent.cache")

#: Comfort pressure below this on an hourly tick with no event -> hold, make no call.
DEFAULT_HOLD_EPSILON = 0.15

_OUTDOOR_BIN_C = 2.0


def hour_band(hour: int) -> str:
    """Coarse time-of-day band aligned with the tariff/carbon shape and the ECM playbook."""
    if hour < 6:
        return "night"
    if hour < 10:
        return "morning"
    if hour < 16:
        return "midday"
    if hour < 22:
        return "peak"
    return "night"


def occupancy_bucket(state: BuildingState) -> str:
    """``empty`` / ``partial`` / ``full`` from total occupants across zones."""
    total = sum(zone.occupancy or 0.0 for zone in state.zones)
    if total <= 0:
        return "empty"
    if total <= 8:
        return "partial"
    return "full"


def outdoor_bin(outdoor_c: float) -> int:
    """Outdoor temperature snapped to a 2 C bin."""
    return int(round(outdoor_c / _OUTDOOR_BIN_C) * _OUTDOOR_BIN_C)


@dataclass(frozen=True)
class CacheKey:
    """The discretized planning situation. Hashable; two near-identical states collide here."""

    hour_band: str
    occupancy: str
    outdoor_bin_c: int
    tariff_band: str
    carbon_band: str

    def label(self) -> str:
        return (
            f"{self.hour_band}/{self.occupancy}/{self.outdoor_bin_c}C/"
            f"t:{self.tariff_band}/c:{self.carbon_band}"
        )


class PlanCache:
    """Discretized plan cache plus the hold pre-filter. Not thread-safe by itself.

    The scheduler runs one planning cycle at a time (epoch-guarded), so a plain dict is fine.
    """

    def __init__(self, *, tariff: dict[int, float], carbon: dict[int, float]) -> None:
        self.tariff = tariff
        self.carbon = carbon
        self._t_lo, self._t_hi = terciles(tariff)
        self._c_lo, self._c_hi = terciles(carbon)
        self._entries: dict[CacheKey, Plan] = {}
        self.calls_made = 0
        self.calls_avoided = 0
        self.holds = 0

    # -- band helpers (shared with the event detector) ---------------------------------

    def tariff_band_at(self, hour: int) -> str:
        return band(self.tariff.get(hour % 24), self._t_lo, self._t_hi)

    def carbon_band_at(self, hour: int) -> str:
        return band(self.carbon.get(hour % 24), self._c_lo, self._c_hi)

    # -- keying ------------------------------------------------------------------------

    def key(self, now: datetime, state: BuildingState) -> CacheKey:
        return CacheKey(
            hour_band=hour_band(now.hour),
            occupancy=occupancy_bucket(state),
            outdoor_bin_c=outdoor_bin(state.outdoor_air_temp_c),
            tariff_band=self.tariff_band_at(now.hour),
            carbon_band=self.carbon_band_at(now.hour),
        )

    # -- lookup / store ----------------------------------------------------------------

    def lookup(self, key: CacheKey) -> Plan | None:
        return self._entries.get(key)

    def store(self, key: CacheKey, plan: Plan) -> None:
        self._entries[key] = plan
        self.calls_made += 1

    def reuse(self, plan: Plan, now: datetime) -> Plan:
        """Replay a cached plan at ``now``: every action window shifted by ``now - created_at``.

        A fresh ``plan_id`` and ``created_at`` so it reads as a new commit; actions, ECMs and
        the horizon are unchanged. Counts as a call avoided.
        """
        shift = now - plan.created_at
        shifted_actions = [
            action.model_copy(update={"start": action.start + shift, "end": action.end + shift})
            for action in plan.actions
        ]
        self.calls_avoided += 1
        return plan.model_copy(
            update={
                "plan_id": Plan().plan_id,  # fresh id via the model's default factory
                "created_at": now,
                "actions": shifted_actions,
            }
        )

    def record_hold(self) -> None:
        self.holds += 1

    # -- the hold pre-filter -----------------------------------------------------------

    @staticmethod
    def should_hold(
        *,
        is_hourly: bool,
        event_fired: bool,
        pressure: float,
        epsilon: float = DEFAULT_HOLD_EPSILON,
    ) -> bool:
        """Deterministic: an hourly tick, no event, nothing drifting -> hold, no call."""
        return is_hourly and not event_fired and pressure < epsilon

    # -- persistence -------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        total = self.calls_made + self.calls_avoided
        pct = round(100 * self.calls_avoided / total) if total else 0
        return {
            "calls_made": self.calls_made,
            "calls_avoided": self.calls_avoided,
            "holds": self.holds,
            "avoided_pct": pct,
            "entries": len(self._entries),
        }

    def save(self, path: Path | str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.stats(), indent=2), encoding="utf-8")
        return out

    def summary(self) -> str:
        s = self.stats()
        return (
            f"calls_made={s['calls_made']} calls_avoided={s['calls_avoided']} "
            f"holds={s['holds']} avoided={s['avoided_pct']}%"
        )


__all__ = [
    "DEFAULT_HOLD_EPSILON",
    "CacheKey",
    "PlanCache",
    "hour_band",
    "occupancy_bucket",
    "outdoor_bin",
]


# Keep asdict importable for callers that want the raw key dict (dashboards, debugging).
def key_as_dict(key: CacheKey) -> dict:
    return asdict(key)
