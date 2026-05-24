"""Wrapper around findmy.py — login, 2FA, and AirTag location fetching.

Uses LocalAnisetteProvider (built into findmy.py) by default — no external
anisette server needed. Falls back to RemoteAnisetteProvider if anisette_url
is explicitly configured.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from findmy import AsyncAppleAccount, FindMyAccessory, LoginState
from findmy.reports.anisette import LocalAnisetteProvider, RemoteAnisetteProvider
from findmy.plist import list_accessories
from findmy import plist as _fm_plist

from . import state

log = logging.getLogger(__name__)

ANISETTE_LIBS_PATH = state.DATA_DIR / "anisette-libs"


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
    """Owns the AsyncAppleAccount + loaded AirTag accessories."""

    def __init__(self):
        self.account: AsyncAppleAccount | None = None
        self.anisette = None
        self.accessories: list[FindMyAccessory] = []
        self.beaconstore_key: bytes | None = None
        self._pending_2fa = None
        self.last_login_state: LoginState = LoginState.LOGGED_OUT

    def _make_anisette(self):
        """Create the appropriate anisette provider."""
        url = state.get().apple.anisette_url
        if url:
            log.info("Using remote anisette: %s", url)
            return RemoteAnisetteProvider(url)
        ANISETTE_LIBS_PATH.mkdir(parents=True, exist_ok=True)
        log.info("Using local anisette (libs cached at %s)", ANISETTE_LIBS_PATH)
        return LocalAnisetteProvider(libs_path=ANISETTE_LIBS_PATH)

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
        """Load AirTag accessories from extracted bundle directory."""
        bs_path = bundle_dir / "BeaconStore.key"
        if not bs_path.exists():
            raise FileNotFoundError(f"BeaconStore.key missing in {bundle_dir}")
        self.beaconstore_key = bs_path.read_bytes()
        if len(self.beaconstore_key) != 32:
            raise ValueError(f"BeaconStore.key is {len(self.beaconstore_key)}B, expected 32")

        # Remove macOS AppleDouble sidecars that confuse findmy's plist parser
        for p in bundle_dir.rglob("._*"):
            if p.is_file():
                p.unlink(missing_ok=True)

        # Monkey-patch findmy's default search path to our bundle location
        _fm_plist._DEFAULT_SEARCH_PATH = bundle_dir
        self.accessories = list_accessories(key=self.beaconstore_key, search_path=bundle_dir)
        log.info("Loaded bundle: %d accessories", len(self.accessories))

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
        log.info("fetch_locations: %d/%d accessories reported", len(out), len(self.accessories))
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
