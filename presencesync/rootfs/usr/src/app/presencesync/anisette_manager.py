# ABOUTME: Manages the embedded anisette server process used for Apple authentication headers.
# ABOUTME: Starts, probes, and stops the local anisette service used by PresenceSync.
"""Manages the embedded anisette-v3-server subprocess.

The anisette server provides Apple authentication headers required for all
iCloud API calls. It runs as a child process inside the container.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

import requests

from . import state

log = logging.getLogger(__name__)

ANISETTE_BINARY = Path("/usr/local/bin/anisette-v3-server")
ANISETTE_PORT = 6969
ANISETTE_URL = f"http://127.0.0.1:{ANISETTE_PORT}"
HEALTH_TIMEOUT = 5
STARTUP_TIMEOUT = 90


class AnisetteManager:
    """Manages the lifecycle of the embedded anisette-v3-server process."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._started = False

    @property
    def url(self) -> str:
        """URL to reach the anisette server."""
        return ANISETTE_URL

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> bool:
        """Start the anisette server subprocess. Returns True if healthy."""
        if self.running:
            return await self.health_check()

        if not ANISETTE_BINARY.exists():
            log.error("anisette binary not found at %s", ANISETTE_BINARY)
            return False

        # Data directory for anisette provisioning state
        data_dir = state.DATA_DIR / "anisette"
        data_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HOME"] = str(data_dir)

        log.info("Starting anisette-v3-server on port %d", ANISETTE_PORT)
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(ANISETTE_BINARY),
                "-p", str(ANISETTE_PORT),
                "--adi-path", str(data_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception:
            log.exception("Failed to start anisette binary")
            return False

        # Start log reader
        asyncio.create_task(self._read_logs())

        # Wait for server to become healthy
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if await self.health_check():
                self._started = True
                log.info("Anisette server healthy at %s", self.url)
                return True
            await asyncio.sleep(1)

        log.error("Anisette server failed to start within %ds", STARTUP_TIMEOUT)
        await self.stop()
        return False

    async def _read_logs(self):
        """Read and log anisette server stdout/stderr."""
        if self._process is None or self._process.stdout is None:
            return
        try:
            async for line in self._process.stdout:
                text = line.decode(errors="replace").rstrip()
                if text:
                    log.debug("[anisette] %s", text)
        except Exception:
            pass

    async def health_check(self) -> bool:
        """Check if the anisette server is responding."""
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: requests.get(self.url, timeout=HEALTH_TIMEOUT)
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def get_headers(self) -> dict[str, str]:
        """Fetch anisette headers from the server."""
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: requests.get(self.url, timeout=HEALTH_TIMEOUT)
        )
        resp.raise_for_status()
        return resp.json()

    async def stop(self):
        """Stop the anisette server subprocess."""
        if self._process is None:
            return
        try:
            self._process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass
        self._process = None
        self._started = False
        log.info("Anisette server stopped")

    async def ensure_running(self) -> bool:
        """Ensure the server is running and healthy; restart if needed."""
        if self.running and await self.health_check():
            return True
        if self.running:
            log.warning("Anisette unhealthy, restarting...")
            await self.stop()
        return await self.start()


# Module-level singleton
_manager: AnisetteManager | None = None


def get() -> AnisetteManager:
    global _manager
    if _manager is None:
        _manager = AnisetteManager()
    return _manager
