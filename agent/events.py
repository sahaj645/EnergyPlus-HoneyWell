"""Event-driven replanning: the ``EventDetector`` Session 5 left as a stub.

The scheduler already replans on startup and hourly. This adds the *reactive* triggers - the
moments where waiting up to an hour for the next tick is too slow:

* **Comfort drift** - an occupied zone with ``|PMV| > 0.4`` or air temperature within 0.3 C of
  its comfort-envelope edge, sustained for **2 consecutive steps** (one noisy sample is not an
  event).
* **Peak approach** - facility demand climbs above 85% of the trailing 7-day peak, i.e. we are
  about to set a new demand charge unless we act.
* **Band change within the hour** - the tariff or grid-carbon band flips in the next hour, so a
  precool/coast decision made against the old band is about to be wrong.
* **EnergyPlus Severe error** - the model just complained; a plan (or a ``patch_model``) may
  have broken something and the planner should look.

Everything is **debounced to 10 simulation-minutes**: a detector that fired keeps quiet for ten
minutes so a single disturbance cannot spam the planner.

``should_trigger`` runs on the callback thread (via ``Scheduler.on_timestep``), so it does only
cheap in-memory comparisons. The one exception - reading the ``.err`` file - is gated on the
file's mtime and a once-per-minute clock, so it is not touched on the hot path most timesteps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from common.log import get_logger
from common.models import BuildingState, ZoneState
from guardian.limits import DEFAULT_CONFIG, EnvelopeConfig

log = get_logger("agent.events")

PMV_LIMIT = 0.4
EDGE_MARGIN_C = 0.3
PEAK_FRACTION = 0.85
DRIFT_STEPS = 2
DEBOUNCE = timedelta(minutes=10)
_PEAK_WINDOW = timedelta(days=7)
_ERR_CHECK_EVERY = timedelta(minutes=1)


def _occupied(zone: ZoneState) -> bool:
    return (zone.occupancy or 0.0) > 0.0


def zone_drifting(zone: ZoneState, envelope: EnvelopeConfig) -> bool:
    """True if an *occupied* zone is at or near a comfort limit."""
    if not _occupied(zone):
        return False
    if zone.pmv is not None and abs(zone.pmv) > PMV_LIMIT:
        return True
    low, high = envelope.band(occupied=True)
    return min(zone.air_temp_c - low, high - zone.air_temp_c) < EDGE_MARGIN_C


def comfort_pressure(state: BuildingState, envelope: EnvelopeConfig | None = None) -> float:
    """A scalar 0..~1 for how close occupied zones are to discomfort.

    ``0`` means every occupied zone sits comfortably mid-band; ``>= 1`` means one is at (or past)
    a PMV or temperature limit. Unoccupied buildings return ``0``. Used both by the drift event
    and by the cache's hold pre-filter (nothing is drifting -> safe to hold).
    """
    envelope = envelope or DEFAULT_CONFIG.envelope
    low, high = envelope.band(occupied=True)
    worst = 0.0
    for zone in state.zones:
        if not _occupied(zone):
            continue
        pmv_term = abs(zone.pmv) / PMV_LIMIT if zone.pmv is not None else 0.0
        dist = min(zone.air_temp_c - low, high - zone.air_temp_c)
        edge_term = max(0.0, (EDGE_MARGIN_C - dist) / EDGE_MARGIN_C)
        worst = max(worst, pmv_term, edge_term)
    return worst


# ``(hour) -> band`` closure so the detector can classify tariff/carbon a specific hour ahead.
BandFn = "Callable[[int], str]"


@dataclass
class DriftEventDetector:
    """Stateful reactive-trigger detector. Satisfies ``scheduler.EventDetector``.

    Constructed with the ambient signals it needs: functions that classify the tariff and carbon
    band for a given clock hour, and (optionally) the EnergyPlus ``.err`` path to watch. Called
    every timestep; it updates its own history and returns whether to replan now.
    """

    tariff_band_at: object  # Callable[[int], str]
    carbon_band_at: object  # Callable[[int], str]
    err_path: Path | None = None
    envelope: EnvelopeConfig = field(default_factory=lambda: DEFAULT_CONFIG.envelope)

    def __post_init__(self) -> None:
        self._drift_run = 0
        self._last_fire: datetime | None = None
        self._power: deque[tuple[datetime, float]] = deque()
        self._last_kw: float | None = None
        self._band_imminent: dict[str, bool] = {"tariff": False, "carbon": False}
        self._err_seen = 0
        self._err_mtime = 0.0
        self._last_err_check: datetime | None = None
        self.last_reason: str | None = None
        self.fire_counts: dict[str, int] = {}

    # -- the scheduler-facing entry point ----------------------------------------------

    def should_trigger(self, now: datetime, state: BuildingState | None) -> bool:
        """Return True if any reactive trigger fires now (respecting the debounce)."""
        reason = self._evaluate(now, state)
        if reason is None:
            return False
        if self._last_fire is not None and now - self._last_fire < DEBOUNCE:
            return False
        self._last_fire = now
        self.last_reason = reason
        self.fire_counts[reason] = self.fire_counts.get(reason, 0) + 1
        log.info("event trigger at %s: %s", now, reason)
        return True

    def _evaluate(self, now: datetime, state: BuildingState | None) -> str | None:
        # Severe errors are checked even without a state - the model can complain at any time.
        if self._severe_error(now):
            return "severe_error"
        if state is None:
            return None

        if self._drift(state):
            return "comfort_drift"
        if self._peak_approach(now, state):
            return "peak_approach"
        band = self._band_change(now)
        if band is not None:
            return band
        return None

    # -- individual triggers -----------------------------------------------------------

    def _drift(self, state: BuildingState) -> bool:
        drifting = any(zone_drifting(zone, self.envelope) for zone in state.zones)
        self._drift_run = self._drift_run + 1 if drifting else 0
        return self._drift_run >= DRIFT_STEPS

    def _peak_approach(self, now: datetime, state: BuildingState) -> bool:
        kw = state.facility_power_w / 1000.0
        rising = self._last_kw is not None and kw > self._last_kw
        self._last_kw = kw
        self._power.append((now, kw))
        cutoff = now - _PEAK_WINDOW
        while self._power and self._power[0][0] < cutoff:
            self._power.popleft()
        # Need a little history before "approaching the peak" means anything.
        if len(self._power) < 6:
            return False
        # "Approaching" = climbing (rising) into the top 15% of the trailing peak. Flat or
        # falling demand is not an approach even when it happens to sit near the peak.
        peak = max(value for _, value in self._power)
        return rising and peak > 0 and kw >= PEAK_FRACTION * peak

    def _band_change(self, now: datetime) -> str | None:
        """Edge-triggered: fire once when a band change first becomes imminent, not for the
        whole hour before it. (Debounce is a floor; this avoids re-firing every step.)"""
        nxt = now + timedelta(hours=1)
        tariff_soon = self.tariff_band_at(now.hour) != self.tariff_band_at(nxt.hour)
        carbon_soon = self.carbon_band_at(now.hour) != self.carbon_band_at(nxt.hour)

        reason: str | None = None
        if tariff_soon and not self._band_imminent["tariff"]:
            reason = "tariff_band_change"
        elif carbon_soon and not self._band_imminent["carbon"]:
            reason = "carbon_band_change"
        self._band_imminent["tariff"] = tariff_soon
        self._band_imminent["carbon"] = carbon_soon
        return reason

    def _severe_error(self, now: datetime) -> bool:
        if self.err_path is None:
            return False
        # Gate the file I/O: at most once per sim-minute, and only re-read if it changed.
        if self._last_err_check is not None and now - self._last_err_check < _ERR_CHECK_EVERY:
            return False
        self._last_err_check = now
        path = Path(self.err_path)
        if not path.is_file():
            return False
        try:
            mtime = path.stat().st_mtime
            if mtime == self._err_mtime:
                return False
            self._err_mtime = mtime
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        count = text.lower().count("** severe") + text.lower().count("**  fatal")
        fired = count > self._err_seen
        self._err_seen = count
        return fired


__all__ = [
    "DEBOUNCE",
    "DRIFT_STEPS",
    "EDGE_MARGIN_C",
    "PEAK_FRACTION",
    "PMV_LIMIT",
    "DriftEventDetector",
    "comfort_pressure",
    "zone_drifting",
]
