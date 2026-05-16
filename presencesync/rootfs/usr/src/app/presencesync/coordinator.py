"""Background polling loop — Apple → MQTT."""
from __future__ import annotations

import asyncio
import logging
import time

from findmy import LoginState

from pathlib import Path

from . import state
from .apple import AppleClient
from .mqtt import MqttPublisher

log = logging.getLogger(__name__)


class Coordinator:
    """Singleton that holds the AppleClient + MqttPublisher and runs the poll loop."""

    def __init__(self):
        self.apple = AppleClient()
        self.mqtt = MqttPublisher()
        self.last_run_unix: int = 0
        self.last_fixes: list = []
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
        if not s.bundle_uploaded:
            log.info("tick skipped: bundle not uploaded yet")
            return
        if self.apple.last_login_state != LoginState.LOGGED_IN:
            log.info("tick skipped: not logged in (state=%s) — sign in via the web UI",
                     self.apple.last_login_state)
            return
        log.info("tick: starting fetch_locations for %d accessories", len(self.apple.accessories))
        fixes = await self.apple.fetch_locations()
        self.last_run_unix = int(time.time())
        self.last_fixes = fixes
        log.info("Apple gateway returned %d fixes", len(fixes))
        for fix in fixes:
            try:
                self.mqtt.publish_fix(fix)
            except Exception:
                log.exception("mqtt publish failed for %s", fix.name)

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
