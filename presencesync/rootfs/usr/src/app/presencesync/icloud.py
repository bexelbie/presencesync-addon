"""Apple iCloud client for FAMILY DEVICES + FRIENDS.

Complements `apple.py` (findmy.py wrapper for AirTags). This module uses
`pyicloud` to talk to `fmipmobile.icloud.com` — the endpoint that powers
the Find My iPhone webapp and Apple's official Find My app's
device/family views.

Why both? findmy.py covers the BLE-relayed offline-finding network
(AirTags, AirPods Pro, tagged accessories). pyicloud covers the
account-side device list (iPhones / iPads / Macs / Watches reporting
their own GPS, plus shared-Family-Sharing devices, plus friends who've
shared their location).

The two libraries use different Apple endpoints and authenticate via
different protocols, so we hold both sessions in parallel. Both persist
to `/data` so the user only does 2FA once per Apple-server invalidation.

Module-load monkey-patches:
- pyicloud.session.PyiCloudSession gets an IPv4-only adapter mounted on
  Apple's three auth hosts (idmsa.apple.com, appleid.apple.com,
  auth.apple.com). Apple's auth servers don't speak IPv6 reliably and
  silently fail to push 2FA codes when contacted over v6. This is the
  fix iCloud3 v3.5 (May 2026) shipped after months of HSA2 breakage.
"""
from __future__ import annotations

import logging
import math
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import urllib3.util.connection
from requests.adapters import HTTPAdapter

from . import state

log = logging.getLogger(__name__)

COOKIE_DIR = state.DATA_DIR / "pyicloud-cookies"


# ── IPv4-only adapter for Apple auth endpoints (iCloud3 v3.5 workaround) ─
class _IPv4OnlyAdapter(HTTPAdapter):
    """Force the underlying urllib3 connection pool to use AF_INET only.

    Apple's auth servers (idmsa/appleid/auth.apple.com) accept IPv6 connections
    but then silently fail to deliver the 2FA push notification flow. Forcing
    DNS to return only IPv4 addresses for those hosts makes HSA2 work again.
    """
    def send(self, request, **kwargs):
        original = urllib3.util.connection.allowed_gai_family
        urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET
        try:
            return super().send(request, **kwargs)
        finally:
            urllib3.util.connection.allowed_gai_family = original


def _install_ipv4_patch_on_pyicloud_session() -> None:
    """Mount IPv4-only adapter on every PyiCloudSession instance.

    Wraps PyiCloudSession.__init__ so all future sessions get the adapter
    bound to the three auth hosts. Other Apple endpoints (fmipmobile, etc.)
    are unaffected and remain free to use IPv4 or IPv6.
    """
    try:
        from pyicloud.session import PyiCloudSession
    except Exception:
        log.warning("pyicloud not importable yet — IPv4 patch deferred")
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
    log.info("pyicloud: IPv4 adapter mounted on idmsa/appleid/auth.apple.com")


# Run at module load so it's in place before any PyiCloudService is constructed.
_install_ipv4_patch_on_pyicloud_session()


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
    battery_level: float | None     # 0.0-1.0, may be None
    battery_status: str | None      # "Charging", "NotCharging", etc.
    device_class: str | None        # "iPhone", "iPad", "Mac", "Watch"
    person_id: str | None = None    # Family member ID, None for self
    person_name: str | None = None  # Family member display name


class ICloudClient:
    """Thin wrapper around pyicloud — auth, persistent cookies, device fetch."""

    def __init__(self):
        self._api = None
        self._last_login_state = "logged_out"
        self._pending_trusted_device: dict | None = None
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
        """Initiate login. Returns the resulting login state."""
        from pyicloud import PyiCloudService
        try:
            self._api = PyiCloudService(
                apple_id=apple_id,
                password=password,
                cookie_directory=str(COOKIE_DIR),
                with_family=True,
            )
        except Exception as err:
            log.exception("pyicloud login failed")
            self._api = None
            raise
        log.info("pyicloud login → state=%s, trusted_session=%s",
                 self.login_state, getattr(self._api, "is_trusted_session", False))
        return self.login_state

    def submit_2fa(self, code: str) -> str:
        if self._api is None:
            raise RuntimeError("not logged in yet")
        ok = self._api.validate_2fa_code(code)
        if not ok:
            raise RuntimeError("pyicloud rejected the 2FA code")
        if not self._api.is_trusted_session:
            log.info("Marking pyicloud session as trusted (avoids 2FA next restart)")
            self._api.trust_session()
        return self.login_state

    def resume_from_cookies(self, apple_id: str, password: str) -> str:
        """Try to resume a saved session from /data/pyicloud-cookies/."""
        # pyicloud auto-loads cookies when you instantiate it; the same call
        # as login() works — just won't ask for 2FA if the session is trusted.
        return self.login(apple_id, password)

    def fetch_devices(self) -> list[DeviceFix]:
        """Get current locations of all owned + family-shared devices."""
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
                    identifier=data.get("id") or data.get("deviceDiscoveryId") or "?",
                    name=data.get("name") or "?",
                    model=data.get("deviceDisplayName") or data.get("rawDeviceModel"),
                    latitude=float(loc["latitude"]),
                    longitude=float(loc["longitude"]),
                    horizontal_accuracy=float(loc.get("horizontalAccuracy") or 0),
                    timestamp_unix=int(ts_ms / 1000) if ts_ms else 0,
                    battery_level=(float(data["batteryLevel"]) if data.get("batteryLevel") is not None else None),
                    battery_status=data.get("batteryStatus"),
                    device_class=data.get("deviceClass"),
                    person_id=data.get("prsId"),
                    person_name=None,  # filled in by caller if family info available
                ))
        except Exception:
            log.exception("pyicloud devices fetch failed")
        log.info("pyicloud devices: %d with location", len(out))
        return out
