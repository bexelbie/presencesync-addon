"""Background polling loop — Apple → MQTT."""
from __future__ import annotations

import asyncio
import logging
import time

from findmy import LoginState

from pathlib import Path

from . import state
from .apple import AppleClient
from .icloud import ICloudClient
from .mqtt import MqttPublisher

log = logging.getLogger(__name__)


class Coordinator:
    """Singleton that holds the AppleClient + MqttPublisher and runs the poll loop."""

    def __init__(self):
        self.apple = AppleClient()
        self.icloud = ICloudClient()
        self.mqtt = MqttPublisher()
        self.last_run_unix: int = 0
        self.last_fixes: list = []          # AirTags + accessories (LocationFix)
        self.last_device_fixes: list = []   # iPhones / iPads / Macs / Watches (DeviceFix)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self):
        if self._task is not None:
            return
        self._stop_event.clear()
        self.mqtt.configure()
        try:
            await self.apple.ensure_account()
        except Exception:
            log.exception("apple.ensure_account failed; will retry in poll loop")

        # Auto-reload the bundle from /data/bundle/ if we have one on disk.
        # /data persists across container restarts / updates, so a user who
        # uploaded once doesn't need to upload again on every addon update.
        if state.get().bundle_uploaded:
            try:
                self.apple.load_bundle(state.BUNDLE_DIR)
            except Exception:
                log.exception("Failed to auto-reload bundle from %s", state.BUNDLE_DIR)

        # Auto-resume the pyicloud session from saved cookies. If cookies in
        # /data/pyicloud-cookies/ are still valid, PyiCloudService instantiation
        # returns a logged_in instance without prompting for 2FA. If cookies
        # have expired Apple-side, we get a needs_2fa state and the UI will
        # surface it. Either way, no manual Log-in click needed across restarts.
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

        self._task = asyncio.create_task(self._run())

    async def _run(self):
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("poll tick raised")
            interval = max(15, int(state.get().tracking.poll_interval_s))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break  # stop signaled
            except asyncio.TimeoutError:
                pass

    async def _tick(self):
        s = state.get()
        self.last_run_unix = int(time.time())

        # AirTags / accessories via findmy.py gateway (needs bundle + login + enabled)
        if not s.tracking.include_airtags:
            log.debug("AirTag fetch skipped: include_airtags=False")
            self.last_fixes = []
        elif s.bundle_uploaded and self.apple.last_login_state == LoginState.LOGGED_IN:
            log.info("tick: fetching AirTag locations for %d accessories",
                     len(self.apple.accessories))
            try:
                fixes = await self.apple.fetch_locations()
                self.last_fixes = fixes
                for fix in fixes:
                    try:
                        self.mqtt.publish_fix(fix)
                    except Exception:
                        log.exception("mqtt publish failed for %s", fix.name)
            except Exception:
                log.exception("AirTag fetch failed")
        else:
            log.debug("AirTag fetch skipped: bundle=%s apple=%s",
                      s.bundle_uploaded, self.apple.last_login_state)

        # Family + owned devices via pyicloud (separate auth + enabled)
        if not s.tracking.include_devices:
            log.debug("iCloud device fetch skipped: include_devices=False")
            self.last_device_fixes = []
        elif self.icloud.login_state == "logged_in":
            log.info("tick: fetching iCloud devices")
            try:
                # pyicloud is sync — run in thread pool to keep async loop responsive
                device_fixes = await asyncio.get_event_loop().run_in_executor(
                    None, self.icloud.fetch_devices
                )
                self.last_device_fixes = device_fixes
                for d in device_fixes:
                    try:
                        self.mqtt.publish_device_fix(d)
                    except Exception:
                        log.exception("mqtt publish failed for device %s", d.name)
            except Exception:
                log.exception("iCloud device fetch failed")
        else:
            log.debug("iCloud fetch skipped: state=%s", self.icloud.login_state)

    async def reload_mqtt(self):
        self.mqtt.configure()

    async def stop(self):
        self._stop_event.set()
        if self._task:
            await self._task
            self._task = None
        self.mqtt.stop()


_coord: Coordinator | None = None


def get() -> Coordinator:
    global _coord
    if _coord is None:
        _coord = Coordinator()
    return _coord
