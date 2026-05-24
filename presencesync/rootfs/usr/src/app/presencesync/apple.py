"""Wrapper around findmy.py — login, 2FA, and AirTag location fetching.

Handles both owned (via FindMy.py master_key rotation) and shared (via
custom sharedFetch API with HKDF bundle keys) accessories.

Uses RemoteAnisetteProvider pointing at the embedded anisette-v3-server.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from findmy import AsyncAppleAccount, FindMyAccessory, LoginState
from findmy.reports.anisette import RemoteAnisetteProvider
from findmy.plist import list_accessories
from findmy import plist as _fm_plist

from . import state
from .shared_fetch import SharedAccessory, fetch_shared_locations, is_shared_plist
from . import anisette_manager

log = logging.getLogger(__name__)

KEYS_DIR = state.DATA_DIR / "keys"


def _get_anisette_dict(url: str) -> dict[str, str]:
    """Fetch anisette headers from GET / as a plain dict."""
    import requests
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


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
    """Owns the AsyncAppleAccount + loaded AirTag accessories (owned + shared)."""

    def __init__(self):
        self.account: AsyncAppleAccount | None = None
        self.anisette = None
        self.accessories: list[FindMyAccessory] = []
        self.shared_accessories: list[SharedAccessory] = []
        self.beaconstore_key: bytes | None = None
        self._pending_2fa = None
        self.last_login_state: LoginState = LoginState.LOGGED_OUT

    def _make_anisette(self):
        """Create anisette provider pointing at embedded or external server."""
        mgr = anisette_manager.get()
        url = mgr.url
        log.info("Using anisette at %s", url)
        return RemoteAnisetteProvider(url)

    async def ensure_account(self) -> None:
        """Create the AsyncAppleAccount if not already initialized."""
        if self.account is not None:
            return
        self.anisette = self._make_anisette()

        saved = state.load_apple_state()
        self.account = AsyncAppleAccount(anisette=self.anisette)

        if isinstance(saved, dict):
            RESTORABLE = {"_uid", "_devid", "_username", "_password",
                          "_login_state", "_login_state_data", "_account_info"}
            applied = []
            for k, v in saved.items():
                if k in RESTORABLE:
                    try:
                        setattr(self.account, k, v)
                        applied.append(k)
                    except Exception:
                        pass
            self.last_login_state = self.account.login_state
            log.info("Resumed Apple account — restored %d fields, state=%s",
                     len(applied), self.last_login_state)

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
        """Load AirTag accessories from extracted bundle directory.

        Handles both:
        - Owned accessories (via FindMy.py's list_accessories + BeaconStore.key)
        - Shared accessories (custom SharedAccessory from plist with wildRootKey)
        """
        self.shared_accessories = []
        self.accessories = []

        # Load shared accessories from any plist that has shared fields
        for plist_path in bundle_dir.glob("*.plist"):
            if is_shared_plist(plist_path):
                shared = SharedAccessory.from_plist(plist_path)
                if shared:
                    self.shared_accessories.append(shared)
                    log.info("Loaded shared accessory: %s (%s)", shared.name, shared.share_id[:8])

        # Load owned accessories (require BeaconStore.key)
        bs_path = bundle_dir / "BeaconStore.key"
        if bs_path.exists():
            self.beaconstore_key = bs_path.read_bytes()
            if len(self.beaconstore_key) != 32:
                log.warning("BeaconStore.key is %dB, expected 32 — skipping owned accessories",
                            len(self.beaconstore_key))
            else:
                # Remove macOS AppleDouble sidecars
                for p in bundle_dir.rglob("._*"):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                _fm_plist._DEFAULT_SEARCH_PATH = bundle_dir
                self.accessories = list_accessories(key=self.beaconstore_key, search_path=bundle_dir)
        else:
            log.info("No BeaconStore.key found — only shared accessories loaded")

        log.info("Loaded: %d owned + %d shared accessories",
                 len(self.accessories), len(self.shared_accessories))

    def load_keys_dir(self) -> None:
        """Load accessories from /data/keys/ (output of export-findmy)."""
        if not KEYS_DIR.exists():
            return
        plists = list(KEYS_DIR.glob("*.plist"))
        if not plists:
            return
        self.load_bundle(KEYS_DIR)

    async def fetch_locations(self) -> list[LocationFix]:
        if self.account is None or self.last_login_state != LoginState.LOGGED_IN:
            return []
        if not self.accessories:
            return []

        sem = asyncio.Semaphore(8)
        timeout_per = 90

        async def _one(acc):
            async with sem:
                try:
                    report = await asyncio.wait_for(
                        self.account.fetch_location(acc), timeout=timeout_per
                    )
                    return acc, report
                except (asyncio.TimeoutError, Exception) as e:
                    log.warning("fetch %s failed: %s", getattr(acc, "name", "?"), e)
                    return acc, None

        results = await asyncio.gather(*[_one(a) for a in self.accessories])
        self._persist()

        out: list[LocationFix] = []
        for acc, report in results:
            if report is None:
                continue
            j = acc.to_json() if hasattr(acc, "to_json") else {}
            out.append(LocationFix(
                identifier=j.get("identifier") or getattr(acc, "name", "unknown"),
                name=j.get("name") or j.get("identifier") or "unknown",
                model=j.get("model"),
                latitude=float(report.latitude),
                longitude=float(report.longitude),
                horizontal_accuracy=float(report.horizontal_accuracy),
                timestamp_unix=int(report.timestamp.timestamp()),
            ))
        log.info("fetch_locations: %d/%d owned accessories reported", len(out), len(self.accessories))
        return out

    async def fetch_shared_location_fixes(self) -> list[LocationFix]:
        """Fetch locations for shared accessories via custom sharedFetch API."""
        if self.account is None or self.last_login_state != LoginState.LOGGED_IN:
            return []
        if not self.shared_accessories:
            return []

        # Get credentials from the account's internal state
        try:
            login_data = self.account._login_state_data
            mobileme = login_data.get("mobileme_data", {})
            tokens = mobileme.get("tokens", {})
            search_party_token = tokens.get("searchPartyToken", "")
            dsid = str(login_data.get("dsid", ""))
            if not search_party_token or not dsid:
                log.warning("Cannot fetch shared: missing searchPartyToken or dsid")
                return []
        except Exception as e:
            log.warning("Cannot fetch shared: failed to get tokens: %s", e)
            return []

        # Get anisette headers from the account's anisette provider
        # This is the proper way — the provider generates user-specific headers
        try:
            if self.anisette is not None:
                anisette_headers = await asyncio.get_event_loop().run_in_executor(
                    None, self.anisette.get_headers
                )
            else:
                mgr = anisette_manager.get()
                anisette_headers = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _get_anisette_dict(mgr.url)
                )
        except Exception as e:
            log.warning("Cannot fetch shared: anisette failed: %s", e)
            return []

        # Call shared fetch (blocking I/O → run in executor)
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: fetch_shared_locations(
                    dsid, search_party_token, anisette_headers, self.shared_accessories
                )
            )
        except Exception:
            log.exception("shared fetch failed")
            return []

        # Convert to LocationFix (use most recent report per accessory)
        out: list[LocationFix] = []
        for name, reports in results.items():
            if not reports:
                continue
            latest = max(reports, key=lambda r: r.timestamp)
            out.append(LocationFix(
                identifier=f"shared-{name}",
                name=name,
                model="AirTag (Shared)",
                latitude=latest.latitude,
                longitude=latest.longitude,
                horizontal_accuracy=float(latest.accuracy),
                timestamp_unix=int(latest.timestamp.timestamp()),
            ))
        log.info("fetch_shared: %d/%d shared accessories reported",
                 len(out), len(self.shared_accessories))
        return out

    def _persist(self) -> None:
        if self.account is None:
            return
        try:
            blob = None
            for attr_name in ("state", "state_info", "export_state", "to_dict"):
                attr = getattr(self.account, attr_name, None)
                if attr is None:
                    continue
                try:
                    blob = attr() if callable(attr) else attr
                    if isinstance(blob, dict):
                        break
                    blob = None
                except Exception:
                    continue
            if blob is None:
                getstate = getattr(self.account, "__getstate__", None)
                if callable(getstate):
                    blob = getstate()
            if blob is not None:
                state.save_apple_state(blob)
        except Exception:
            log.warning("Could not persist Apple state", exc_info=True)
