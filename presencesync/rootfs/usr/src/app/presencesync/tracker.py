"""Per-device intelligence for iDevice tracking.

Implements smart polling behaviors:
- Dynamic interval based on distance from home
- GPS flap suppression (consecutive readings before state flip)
- Battery-aware polling (longer intervals when battery is low)
- Stationary zone detection (extend intervals when not moving)
- Stale/offline detection (mark unavailable after configurable timeout)
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from . import state
from .icloud import DeviceFix

log = logging.getLogger(__name__)


class PublishAction(Enum):
    """What the coordinator should do after ingesting a fix."""
    PUBLISH = "publish"         # state changed or periodic update — publish to MQTT
    SUPPRESS = "suppress"       # flap suppression — don't publish state change yet
    UNAVAILABLE = "unavailable" # mark entity as unavailable


@dataclass
class PublishDecision:
    action: PublishAction
    state: str                 # "home", "not_home", or "unavailable"
    fix: DeviceFix | None = None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class DeviceTracker:
    """Per-device state machine for intelligent tracking."""

    def __init__(self, device_id: str, device_name: str):
        self.device_id = device_id
        self.device_name = device_name
        self.history: deque[DeviceFix] = deque(maxlen=10)
        self.last_published_state: str = "unknown"
        self.last_movement_time: float = time.time()
        self.last_fix_time: float = 0.0
        self.distance_from_home: float = float("inf")
        self.battery_level: float | None = None
        self.battery_status: str | None = None
        self._pending_state: str | None = None
        self._pending_count: int = 0

    def ingest(self, fix: DeviceFix) -> PublishDecision:
        """Process a new fix and decide whether to publish."""
        self.history.append(fix)
        self.last_fix_time = time.time()
        self.battery_level = fix.battery_level
        self.battery_status = fix.battery_status

        cfg = state.get().tracking
        home = state.get().home

        # Compute distance from home
        if home.latitude:
            self.distance_from_home = _haversine_m(
                fix.latitude, fix.longitude,
                home.latitude, home.longitude
            )
        else:
            self.distance_from_home = float("inf")

        # Detect movement (for stationary detection)
        if len(self.history) >= 2:
            prev = self.history[-2]
            displacement = _haversine_m(
                prev.latitude, prev.longitude,
                fix.latitude, fix.longitude
            )
            # "Meaningful" = moved more than accuracy + 50m buffer
            if displacement > (fix.horizontal_accuracy + 50):
                self.last_movement_time = time.time()

        # Compute desired state
        new_state = "home" if self.distance_from_home <= home.radius_m else "not_home"

        # Flap suppression: require consecutive readings before flipping
        if not cfg.dynamic_polling:
            # No intelligence — always publish
            self.last_published_state = new_state
            return PublishDecision(action=PublishAction.PUBLISH, state=new_state, fix=fix)

        # First fix ever — always publish (no suppression for initial state)
        if self.last_published_state == "unknown":
            self.last_published_state = new_state
            return PublishDecision(action=PublishAction.PUBLISH, state=new_state, fix=fix)

        if new_state == self.last_published_state:
            # No state change — reset pending counter, publish update
            self._pending_state = None
            self._pending_count = 0
            return PublishDecision(action=PublishAction.PUBLISH, state=new_state, fix=fix)

        # State is different from last published — count consecutive readings
        if new_state == self._pending_state:
            self._pending_count += 1
        else:
            self._pending_state = new_state
            self._pending_count = 1

        if self._pending_count >= cfg.flap_suppression_count:
            # Enough consecutive readings agree — flip state
            log.info("Device %s: state flip %s → %s (after %d readings)",
                     self.device_name, self.last_published_state, new_state,
                     self._pending_count)
            self.last_published_state = new_state
            self._pending_state = None
            self._pending_count = 0
            return PublishDecision(action=PublishAction.PUBLISH, state=new_state, fix=fix)

        # Not enough consecutive readings yet — suppress the flip
        log.debug("Device %s: suppressing %s → %s (%d/%d)",
                  self.device_name, self.last_published_state, new_state,
                  self._pending_count, cfg.flap_suppression_count)
        return PublishDecision(
            action=PublishAction.SUPPRESS,
            state=self.last_published_state,
            fix=fix,
        )

    def is_stale(self) -> bool:
        """True if the device hasn't reported in longer than the stale threshold."""
        cfg = state.get().tracking
        threshold_s = max(600, cfg.stale_threshold_hours * 3600)  # min 10 minutes
        if self.last_fix_time == 0:
            return False  # never seen — not stale, just unknown
        return (time.time() - self.last_fix_time) > threshold_s

    def next_poll_seconds(self) -> int:
        """Compute the optimal next-poll interval for this device."""
        cfg = state.get().tracking

        if not cfg.dynamic_polling:
            return cfg.poll_interval_s

        # Base interval from distance
        if self.distance_from_home < 500:
            base = 30      # close: poll frequently
        elif self.distance_from_home < 2000:
            base = 60      # nearby
        elif self.distance_from_home < 5000:
            base = 120     # medium distance
        else:
            base = 300     # far away

        # Stationary detection: if no meaningful movement, extend
        minutes_since_movement = (time.time() - self.last_movement_time) / 60
        if minutes_since_movement > cfg.stationary_threshold_minutes:
            base = max(base, 300)  # at least 5 minutes if stationary
            log.debug("Device %s stationary for %.0fmin → interval %ds",
                      self.device_name, minutes_since_movement, base)

        # Battery awareness
        if self.battery_level is not None:
            is_charging = (self.battery_status or "").lower() in ("charging", "full")
            if not is_charging:
                if self.battery_level < cfg.battery_critical_threshold:
                    base *= 4
                elif self.battery_level < cfg.battery_low_threshold:
                    base *= 2

        # Clamp to reasonable bounds
        return max(15, min(base, 900))  # 15s to 15min


class TrackerManager:
    """Manages all DeviceTracker instances."""

    def __init__(self):
        self._trackers: dict[str, DeviceTracker] = {}

    def get_tracker(self, device_id: str, device_name: str = "") -> DeviceTracker:
        """Get or create a tracker for a device."""
        if device_id not in self._trackers:
            self._trackers[device_id] = DeviceTracker(device_id, device_name)
        return self._trackers[device_id]

    def ingest(self, fix: DeviceFix) -> PublishDecision:
        """Route a fix to its device tracker and return the decision."""
        tracker = self.get_tracker(fix.identifier, fix.name)
        return tracker.ingest(fix)

    def next_poll_seconds(self) -> int:
        """Get the minimum poll interval across all tracked devices."""
        if not self._trackers:
            return state.get().tracking.poll_interval_s
        intervals = [t.next_poll_seconds() for t in self._trackers.values()]
        return min(intervals)

    def stale_devices(self) -> list[DeviceTracker]:
        """Return trackers that have gone stale."""
        return [t for t in self._trackers.values() if t.is_stale()]
