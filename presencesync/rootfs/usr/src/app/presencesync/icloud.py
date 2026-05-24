"""Apple iCloud client for FAMILY DEVICES.

Uses pyicloud (timlaing fork) to talk to fmipmobile.icloud.com — the endpoint
that powers Find My iPhone. Provides: device locations, battery levels, and
play_sound capability.

Module-load monkey-patches:
- PyiCloudSession gets an IPv4-only adapter on Apple's auth hosts.
  Apple's auth servers silently fail 2FA push over IPv6.
"""
from __future__ import annotations

import logging
import socket
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

    def fetch_devices(self) -> list[DeviceFix]:
        """Get current locations of all owned + family devices."""
        if self._api is None or self.login_state != "logged_in":
            return []
        out: list[DeviceFix] = []
        try:
            for dev in self._api.devices:
                data = dev.data if hasattr(dev, "data") else {}
                loc = data.get("location") or {}
                if not loc or loc.get("latitude") is None:
                    continue
                ts_ms = loc.get("timeStamp") or 0
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
                ))
        except Exception:
            log.exception("pyicloud devices fetch failed")
        log.info("pyicloud: %d devices with location", len(out))
        return out

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
