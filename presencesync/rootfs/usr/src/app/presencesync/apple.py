"""Wrapper around findmy.py — login, 2FA, and location fetching with persisted auth."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from findmy import AsyncAppleAccount, FindMyAccessory, LoginState
from findmy.reports.anisette import RemoteAnisetteProvider
from findmy.plist import list_accessories

from . import state

log = logging.getLogger(__name__)


@dataclass
class LocationFix:
    identifier: str
    name: str
    model: str | None
    latitude: float
    longitude: float
    horizontal_accuracy: float
    timestamp_unix: int


class AppleClient:
    """Owns the AsyncAppleAccount + the loaded OwnedBeacons. Tracks login state."""

    def __init__(self):
        self.account: AsyncAppleAccount | None = None
        self.anisette: RemoteAnisetteProvider | None = None
        self.accessories: list[FindMyAccessory] = []
        self.beaconstore_key: bytes | None = None
        self._pending_2fa: object | None = None  # AsyncTrustedDeviceSecondFactor or AsyncSmsSecondFactor
        self.last_login_state: LoginState = LoginState.LOGGED_OUT

    async def ensure_account(self) -> None:
        """Create the AsyncAppleAccount if not already, attaching the anisette provider."""
        if self.account is not None:
            return
        anisette_url = state.get().apple.anisette_url or os.environ.get("PRESENCESYNC_ANISETTE_URL", "")
        if not anisette_url:
            raise RuntimeError("anisette_url is not configured")
        self.anisette = RemoteAnisetteProvider(anisette_url)
        # Try to resume from persisted state
        saved = state.load_apple_state()
        if saved:
            try:
                self.account = AsyncAppleAccount(anisette=self.anisette, state_info=saved)
                self.last_login_state = self.account.login_state
                log.info("Resumed Apple account from saved state: %s", self.last_login_state)
                return
            except Exception:
                log.exception("Failed to resume saved Apple state; will require login again")
                state.clear_apple_state()
        self.account = AsyncAppleAccount(anisette=self.anisette)

    async def login(self, username: str, password: str) -> LoginState:
        await self.ensure_account()
        assert self.account is not None
        result = await self.account.login(username, password)
        self.last_login_state = result
        self._persist()
        return result

    async def request_2fa(self, method_index: int = 0) -> None:
        assert self.account is not None
        methods = await self.account.get_2fa_methods()
        if not methods:
            raise RuntimeError("no 2FA methods available")
        method = methods[min(method_index, len(methods) - 1)]
        await method.request()
        self._pending_2fa = method

    async def submit_2fa(self, code: str) -> LoginState:
        assert self.account is not None
        if self._pending_2fa is None:
            methods = await self.account.get_2fa_methods()
            if not methods:
                raise RuntimeError("no 2FA methods to submit against")
            self._pending_2fa = methods[0]
        result = await self._pending_2fa.submit(code)
        self.last_login_state = result
        self._pending_2fa = None
        self._persist()
        return result

    def load_bundle(self, bundle_dir: Path) -> None:
        """Decrypt BeaconStore.key and load all FindMyAccessory objects via findmy.plist.

        AirTags use rolling keys (the advertised public key rotates ~every 15 min),
        derived from a master key + shared secret. FindMyAccessory wraps that
        rolling-key derivation; passing it to fetch_location lets findmy.py
        figure out which historical keys to query Apple's gateway for.
        """
        bs_path = bundle_dir / "BeaconStore.key"
        if not bs_path.exists():
            raise FileNotFoundError(f"BeaconStore.key missing in {bundle_dir}")
        self.beaconstore_key = bs_path.read_bytes()
        if len(self.beaconstore_key) != 32:
            raise ValueError(f"BeaconStore.key is {len(self.beaconstore_key)}B, expected 32")

        # findmy expects the bundle to look like ~/Library/com.apple.icloud.searchpartyd/
        # i.e. OwnedBeacons/, BeaconNamingRecord/, KeyAlignmentRecords/ at the root.
        # Our bundle.tar.gz lays them out exactly that way.
        self.accessories = list_accessories(key=self.beaconstore_key, search_path=bundle_dir)
        log.info("Loaded bundle: %d accessories", len(self.accessories))
        for a in self.accessories:
            j = a.to_json() if hasattr(a, "to_json") else {}
            log.info("  - %s (%s) %s", j.get("name") or "?", j.get("model") or "?", j.get("identifier") or "?")

    async def fetch_locations(self) -> list[LocationFix]:
        if self.account is None or self.last_login_state != LoginState.LOGGED_IN:
            return []
        if not self.accessories:
            return []

        try:
            reports = await self.account.fetch_location(self.accessories)
        except Exception:
            log.exception("fetch_location failed")
            return []
        self._persist()  # may include refreshed tokens

        out: list[LocationFix] = []
        if isinstance(reports, dict):
            for acc, report in reports.items():
                if report is None:
                    continue
                j = acc.to_json() if hasattr(acc, "to_json") else {}
                ident = j.get("identifier") or getattr(acc, "name", "unknown")
                name = j.get("name") or ident
                model = j.get("model")
                out.append(LocationFix(
                    identifier=ident,
                    name=name,
                    model=model,
                    latitude=float(report.latitude),
                    longitude=float(report.longitude),
                    horizontal_accuracy=float(report.horizontal_accuracy),
                    timestamp_unix=int(report.timestamp.timestamp()),
                ))
        elif reports is not None:
            # Single accessory case — wrap into a 1-element dict equivalent
            j = self.accessories[0].to_json() if self.accessories else {}
            out.append(LocationFix(
                identifier=j.get("identifier", "?"),
                name=j.get("name") or j.get("identifier", "?"),
                model=j.get("model"),
                latitude=float(reports.latitude),
                longitude=float(reports.longitude),
                horizontal_accuracy=float(reports.horizontal_accuracy),
                timestamp_unix=int(reports.timestamp.timestamp()),
            ))
        log.info("fetch_locations got %d location reports", len(out))
        return out

    def _persist(self) -> None:
        if self.account is None:
            return
        try:
            getstate = getattr(self.account, "__getstate__", None)
            if callable(getstate):
                state.save_apple_state(getstate())
        except Exception:
            log.debug("Could not persist Apple state", exc_info=True)
