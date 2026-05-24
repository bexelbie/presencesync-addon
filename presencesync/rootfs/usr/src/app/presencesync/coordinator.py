"""Background polling loops — Apple → MQTT.

Two independent loops:
- AirTag loop: slow cadence (default 10min), accelerates on movement
- iDevice loop: governed by tracker.py intelligence (battery/distance/stationary-aware)
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from findmy import LoginState

from . import state
from .apple import AppleClient, LocationFix
from .icloud import ICloudClient
from .mqtt import MqttPublisher
from .tracker import TrackerManager, PublishAction

log = logging.getLogger(__name__)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Coordinator:
    """Singleton that holds the AppleClient + MqttPublisher and runs two poll loops."""

    def __init__(self):
        self.apple = AppleClient()
        self.icloud = ICloudClient()
        self.mqtt = MqttPublisher()
        self.tracker_mgr = TrackerManager()
        self.last_run_unix: int = 0
        self.last_fixes: list = []          # AirTags + accessories (LocationFix)
        self.last_device_fixes: list = []   # iPhones / iPads / Macs / Watches (DeviceFix)
        self._airtag_task: asyncio.Task | None = None
        self._idevice_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

        # Per-AirTag last-known positions for movement detection
        self._airtag_last_pos: dict[str, tuple[float, float]] = {}

    async def start(self):
        if self._airtag_task is not None or self._idevice_task is not None:
            return
        self._stop_event.clear()
        self.mqtt.configure()
        try:
            await self.apple.ensure_account()
        except Exception:
            log.exception("apple.ensure_account failed; will retry in poll loop")

        # Auto-reload the bundle from /data/bundle/ if we have one on disk.
        if state.get().bundle_uploaded:
            try:
                self.apple.load_bundle(state.BUNDLE_DIR)
            except Exception:
                log.exception("Failed to auto-reload bundle from %s", state.BUNDLE_DIR)

        # Auto-resume the pyicloud session from saved cookies.
        s = state.get()
        if s.tracking.include_devices and s.apple.username and s.apple.password:
            log.info("icloud: attempting auto-resume from saved cookies")
            try:
                ic_state = await asyncio.get_event_loop().run_in_executor(
                    None, self.icloud.login, s.apple.username, s.apple.password
                )
                log.info("icloud auto-resume: %s", ic_state)
            except Exception:
                log.exception("icloud auto-resume failed (will need fresh login)")

        self._airtag_task = asyncio.create_task(self._run_airtag_loop())
        self._idevice_task = asyncio.create_task(self._run_idevice_loop())

    # ── AirTag Loop ──────────────────────────────────────────────────────────

    async def _run_airtag_loop(self):
        """Poll AirTags on a slow cadence, accelerating on movement."""
        while not self._stop_event.is_set():
            movement_detected = False
            try:
                movement_detected = await self._tick_airtags()
            except Exception:
                log.exception("AirTag poll tick raised")

            s = state.get().tracking
            if movement_detected:
                interval = max(60, s.airtag_movement_interval_s)
                log.info("AirTag movement detected → next poll in %ds", interval)
            else:
                interval = max(60, s.airtag_poll_interval_s)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

    async def _tick_airtags(self) -> bool:
        """Fetch AirTag locations. Returns True if significant movement detected."""
        s = state.get()
        if not s.tracking.include_airtags:
            log.debug("AirTag fetch skipped: include_airtags=False")
            self.last_fixes = []
            return False

        if not (s.bundle_uploaded and self.apple.last_login_state == LoginState.LOGGED_IN):
            log.debug("AirTag fetch skipped: bundle=%s apple=%s",
                      s.bundle_uploaded, self.apple.last_login_state)
            return False

        log.info("airtag tick: fetching locations for %d accessories",
                 len(self.apple.accessories))
        fixes = await self.apple.fetch_locations()
        self.last_fixes = fixes
        self.last_run_unix = int(time.time())

        movement_detected = False
        threshold = s.tracking.airtag_movement_threshold_m

        for fix in fixes:
            try:
                self.mqtt.publish_fix(fix)
            except Exception:
                log.exception("mqtt publish failed for %s", fix.name)

            # Movement detection
            prev = self._airtag_last_pos.get(fix.identifier)
            if prev is not None:
                dist = _haversine_m(prev[0], prev[1], fix.latitude, fix.longitude)
                if dist > max(threshold, fix.horizontal_accuracy):
                    log.info("AirTag %s moved %.0fm (threshold %.0fm)",
                             fix.name, dist, threshold)
                    movement_detected = True
            self._airtag_last_pos[fix.identifier] = (fix.latitude, fix.longitude)

        return movement_detected

    # ── iDevice Loop ─────────────────────────────────────────────────────────

    async def _run_idevice_loop(self):
        """Poll iDevices with dynamic intervals governed by tracker intelligence."""
        while not self._stop_event.is_set():
            try:
                await self._tick_idevices()
            except Exception:
                log.exception("iDevice poll tick raised")

            # Dynamic interval from tracker (min across all devices)
            interval = self.tracker_mgr.next_poll_seconds()
            log.debug("iDevice loop: next poll in %ds", interval)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

    async def _tick_idevices(self):
        """Fetch iDevice locations via pyicloud, filtered through tracker intelligence."""
        s = state.get()
        if not s.tracking.include_devices:
            log.debug("iCloud device fetch skipped: include_devices=False")
            self.last_device_fixes = []
            return

        if self.icloud.login_state != "logged_in":
            log.debug("iCloud fetch skipped: state=%s", self.icloud.login_state)
            return

        log.info("idevice tick: fetching iCloud devices")
        # pyicloud is sync — run in thread pool to keep async loop responsive
        device_fixes = await asyncio.get_event_loop().run_in_executor(
            None, self.icloud.fetch_devices
        )
        self.last_device_fixes = device_fixes
        self.last_run_unix = int(time.time())

        for d in device_fixes:
            decision = self.tracker_mgr.ingest(d)
            if decision.action == PublishAction.PUBLISH:
                try:
                    self.mqtt.publish_device_fix(d)
                except Exception:
                    log.exception("mqtt publish failed for device %s", d.name)
            elif decision.action == PublishAction.SUPPRESS:
                log.debug("Suppressed state flip for %s (flap protection)", d.name)

        # Check for stale devices and mark unavailable
        for tracker in self.tracker_mgr.stale_devices():
            log.warning("Device %s is stale (no fix for >%.1fh) — marking unavailable",
                        tracker.device_name, s.tracking.stale_threshold_hours)
            self.mqtt.publish_unavailable(tracker.device_id)

    # ── Public API ───────────────────────────────────────────────────────────

    async def poll_now(self):
        """Trigger immediate poll of both loops (called by /api/poll-now)."""
        tasks = []
        if state.get().tracking.include_airtags:
            tasks.append(self._tick_airtags())
        if state.get().tracking.include_devices:
            tasks.append(self._tick_idevices())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reload_mqtt(self):
        self.mqtt.configure()

    async def stop(self):
        self._stop_event.set()
        for task in (self._airtag_task, self._idevice_task):
            if task:
                await task
        self._airtag_task = None
        self._idevice_task = None
        self.mqtt.stop()


_coord: Coordinator | None = None


def get() -> Coordinator:
    global _coord
    if _coord is None:
        _coord = Coordinator()
    return _coord
