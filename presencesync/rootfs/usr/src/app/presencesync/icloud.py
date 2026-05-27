# ABOUTME: Talks to Apple's Find My iPhone service for owned and family device state.
# ABOUTME: Supports active refreshes that wake devices and polls that read cached Apple data.
from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass

import urllib3.util.connection
from requests.adapters import HTTPAdapter

from . import state

log = logging.getLogger(__name__)

COOKIE_DIR = state.DATA_DIR / "pyicloud-cookies"


# ── IPv4-only adapter for Apple auth endpoints ───────────────────────────────

class _IPv4OnlyAdapter(HTTPAdapter):
    """Force AF_INET for Apple auth hosts (iCloud3 v3.5 workaround)."""
    def send(self, request, **kwargs):
        original = urllib3.util.connection.allowed_gai_family
        urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
        try:
            return super().send(request, **kwargs)
        finally:
            urllib3.util.connection.allowed_gai_family = original


def _install_ipv4_patch() -> None:
    try:
        from pyicloud.session import PyiCloudSession
    except Exception:
        return
    if getattr(PyiCloudSession, "_presencesync_ipv4_patched", False):
        return
    _orig_init = PyiCloudSession.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        adapter = _IPv4OnlyAdapter()
        for host in ("https://idmsa.apple.com",
                     "https://appleid.apple.com",
                     "https://auth.apple.com"):
            self.mount(host, adapter)

    PyiCloudSession.__init__ = _patched_init
    PyiCloudSession._presencesync_ipv4_patched = True
    log.info("pyicloud: IPv4 adapter mounted on Apple auth hosts")


_install_ipv4_patch()


@dataclass
class DeviceFix:
    """A location report for a family/owned Apple device."""
    identifier: str
    name: str
    model: str | None
    latitude: float
    longitude: float
    horizontal_accuracy: float
    timestamp_unix: int
    battery_level: float | None
    battery_status: str | None
    device_class: str | None
    ba_uuid: str | None = None  # CloudKit beacon record ID (matches metadata)
    owner: str | None = None  # Family member name/email (from FMiP prsId lookup)


class ICloudClient:
    """Thin wrapper around pyicloud — auth, cookies, device fetch, play_sound."""

    def __init__(self):
        self._api = None
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def login_state(self) -> str:
        if self._api is None:
            return "logged_out"
        if getattr(self._api, "requires_2fa", False):
            return "needs_2fa"
        if getattr(self._api, "requires_2sa", False):
            return "needs_2sa"
        return "logged_in"

    def login(self, apple_id: str, password: str) -> str:
        from pyicloud import PyiCloudService
        try:
            self._api = PyiCloudService(
                apple_id=apple_id,
                password=password,
                cookie_directory=str(COOKIE_DIR),
                with_family=True,
                refresh_interval=86400,  # disable internal poll thread; we drive timing
            )
        except Exception:
            log.exception("pyicloud login failed")
            self._api = None
            raise
        log.info("pyicloud login → state=%s", self.login_state)
        return self.login_state

    def submit_2fa(self, code: str) -> str:
        if self._api is None:
            raise RuntimeError("not logged in yet")
        ok = self._api.validate_2fa_code(code)
        if not ok:
            raise RuntimeError("pyicloud rejected the 2FA code")
        if not self._api.is_trusted_session:
            self._api.trust_session()
        return self.login_state

    def _extract_fixes(self, mgr) -> list[DeviceFix]:
        out: list[DeviceFix] = []
        user_info = getattr(mgr, "_user_info", None) or {}
        members_info = user_info.get("membersInfo") or {}
        owner_by_prs_id = {}
        for prs_id, info in members_info.items():
            first = info.get("firstName", "")
            last = info.get("lastName", "")
            full_name = f"{first} {last}".strip()
            if full_name:
                owner_by_prs_id[prs_id] = full_name
        user_prs_id = user_info.get("prsId")

        for dev in mgr:
            data = dev.data if hasattr(dev, "data") else {}
            loc = data.get("location") or {}
            if not loc or loc.get("latitude") is None:
                continue
            ts_ms = loc.get("timeStamp") or 0
            prs_id = data.get("prsId")
            owner = None
            if prs_id and prs_id != user_prs_id:
                owner = owner_by_prs_id.get(prs_id)
            out.append(DeviceFix(
                identifier=data.get("id") or data.get("deviceDiscoveryId") or "unknown",
                name=data.get("name") or "unknown",
                model=data.get("deviceDisplayName") or data.get("rawDeviceModel"),
                latitude=float(loc["latitude"]),
                longitude=float(loc["longitude"]),
                horizontal_accuracy=float(loc.get("horizontalAccuracy") or 0),
                timestamp_unix=int(ts_ms / 1000) if ts_ms else 0,
                battery_level=(float(data["batteryLevel"]) if data.get("batteryLevel") is not None else None),
                battery_status=data.get("batteryStatus"),
                device_class=data.get("deviceClass"),
                ba_uuid=data.get("baUUID") or None,
                owner=owner,
            ))
        return out

    def refresh_devices(self) -> list[DeviceFix]:
        """Get current locations of all owned + family devices."""
        if self._api is None or self.login_state != "logged_in":
            return []
        out: list[DeviceFix] = []
        try:
            mgr = self._api.devices  # cached property, no network call

            # Instrument: which endpoint will be hit and how large is serverContext?
            ctx = getattr(mgr, "_server_ctx", None)
            ctx_size = len(str(ctx)) if ctx else 0
            url_type = "refreshClient" if ctx else "initClient"
            log.info("pyicloud fetch: url=%s, serverContext_size=%d", url_type, ctx_size)

            # Explicitly refresh (we disabled the background thread)
            t0 = time.monotonic()
            mgr._refresh_client(locate=True)
            t_refresh = time.monotonic() - t0

            out = self._extract_fixes(mgr)

            # RSS memory (Linux)
            rss_mb = 0.0
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_mb = int(line.split()[1]) / 1024
                            break
            except OSError:
                pass

            log.info("pyicloud: %d devices, refresh=%.1fs (%s), rss=%.0fMB",
                     len(out), t_refresh, url_type, rss_mb)
        except Exception:
            log.exception("pyicloud devices fetch failed")
            raise
        return out

    def poll_devices(self) -> list[DeviceFix]:
        """Get cached locations of all owned + family devices without waking them."""
        if self._api is None or self.login_state != "logged_in":
            return []
        out: list[DeviceFix] = []
        try:
            mgr = self._api.devices  # cached property, no network call

            # Instrument: which endpoint will be hit and how large is serverContext?
            ctx = getattr(mgr, "_server_ctx", None)
            ctx_size = len(str(ctx)) if ctx else 0
            url_type = "refreshClient" if ctx else "initClient"
            log.info("pyicloud fetch: url=%s, serverContext_size=%d", url_type, ctx_size)

            # Explicitly poll cached data (we disabled the background thread)
            t0 = time.monotonic()
            mgr._refresh_client(locate=False)
            t_refresh = time.monotonic() - t0

            out = self._extract_fixes(mgr)

            # RSS memory (Linux)
            rss_mb = 0.0
            try:
                with open("/proc/self/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_mb = int(line.split()[1]) / 1024
                            break
            except OSError:
                pass

            log.info("pyicloud: %d devices, refresh=%.1fs (%s), rss=%.0fMB",
                     len(out), t_refresh, url_type, rss_mb)
        except Exception:
            log.exception("pyicloud devices fetch failed")
            raise
        return out

    def reset_server_context(self) -> None:
        """Clear FMiP serverContext — next fetch uses initClient (fresh session)."""
        if self._api is None:
            return
        mgr = self._api.devices
        old_size = len(str(getattr(mgr, "_server_ctx", None) or ""))
        mgr._server_ctx = None
        log.info("pyicloud: serverContext reset (was %d chars) → next call uses initClient",
                 old_size)

    def play_sound(self, device_id: str, subject: str = "Find My iPhone Alert") -> bool:
        """Trigger Find My alert sound on a device."""
        if self._api is None or self.login_state != "logged_in":
            return False
        try:
            for dev in self._api.devices:
                data = dev.data if hasattr(dev, "data") else {}
                dev_id = data.get("id") or data.get("deviceDiscoveryId")
                if dev_id == device_id:
                    dev.play_sound(subject=subject)
                    log.info("play_sound: %s (%s)", data.get("name", "?"), device_id)
                    return True
        except Exception:
            log.exception("play_sound failed for %s", device_id)
        return False
