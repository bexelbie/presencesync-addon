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
from findmy import plist as _fm_plist  # for monkey-patching _DEFAULT_SEARCH_PATH

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
        # Try to resume from persisted state. We only attempt this if the saved
        # blob looks like an AccountStateMapping — older versions wrote a partial
        # shape that AsyncAppleAccount rejects with KeyError 'ids' and the close
        # path also tries to use _closed which isn't set yet. Validate first.
        saved = state.load_apple_state()
        is_valid_saved = isinstance(saved, dict) and "ids" in saved and "account" in saved
        if saved and not is_valid_saved:
            log.warning("Saved Apple state is malformed; clearing")
            state.clear_apple_state()
            saved = None
        if saved:
            try:
                self.account = AsyncAppleAccount(anisette=self.anisette, state_info=saved)
                self.last_login_state = self.account.login_state
                log.info("Resumed Apple account from saved state: %s", self.last_login_state)
                return
            except Exception:
                log.exception("Failed to resume saved Apple state; will require login again")
                state.clear_apple_state()
                self.account = None
        if self.account is None:
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

        # macOS tar pollutes bundles with AppleDouble `._*` sidecars holding
        # extended attributes. findmy.plist.list_accessories doesn't filter
        # them and chokes when it tries to parse one as a plist. Strip them
        # before letting findmy walk the tree.
        sidecars = [p for p in bundle_dir.rglob("._*") if p.is_file()]
        for p in sidecars:
            p.unlink(missing_ok=True)
        if sidecars:
            log.info("Removed %d AppleDouble sidecar(s) before findmy.list_accessories", len(sidecars))

        # findmy expects the bundle to look like ~/Library/com.apple.icloud.searchpartyd/
        # i.e. OwnedBeacons/, BeaconNamingRecord/, KeyAlignmentRecords/ at the root.
        # Our bundle.tar.gz lays them out exactly that way.
        # findmy.plist.list_accessories has a bug: it accepts search_path but DOESN'T
        # forward it to _get_accessory_name / _get_alignment_plist, which fall back
        # to ~/Library/com.apple.icloud.searchpartyd (doesn't exist in this container).
        # Monkey-patch _DEFAULT_SEARCH_PATH so the helpers find our bundle.
        _fm_plist._DEFAULT_SEARCH_PATH = bundle_dir
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

        # findmy.py processes RollingKeyPairSource accessories SERIALLY when given a
        # list, and each one iterates ~7 days × 96 slots looking backwards for
        # reports. Per-accessory time can be a minute or more for the very first
        # fetch. Parallelize at our level and bound each accessory's time so a
        # single slow one doesn't block the whole tick.
        sem = asyncio.Semaphore(8)
        per_accessory_timeout = 90  # seconds

        async def _one(acc):
            async with sem:
                acc_name = getattr(acc, "name", None) or (
                    acc.to_json().get("name") if hasattr(acc, "to_json") else None
                ) or "?"
                t_start = time.time()
                try:
                    report = await asyncio.wait_for(
                        self.account.fetch_location(acc),
                        timeout=per_accessory_timeout,
                    )
                    log.info("  fetch %s done in %.1fs → %s",
                             acc_name, time.time() - t_start,
                             "report" if report is not None else "no report")
                    return acc, report
                except asyncio.TimeoutError:
                    log.warning("  fetch %s TIMED OUT after %ds — skipping this cycle",
                                acc_name, per_accessory_timeout)
                    return acc, None
                except Exception as err:
                    log.warning("  fetch %s FAILED: %s: %s",
                                acc_name, type(err).__name__, err)
                    return acc, None

        t0 = time.time()
        log.info("starting parallel fetch of %d accessories (sem=8, timeout=%ds each)",
                 len(self.accessories), per_accessory_timeout)
        tasks = [_one(a) for a in self.accessories]
        results = await asyncio.gather(*tasks)
        with_report = sum(1 for _, r in results if r is not None)
        log.info("fetch_locations: %d/%d accessories returned a report in %.1fs",
                 with_report, len(results), time.time() - t0)
        self._persist()  # may include refreshed tokens

        out: list[LocationFix] = []
        for acc, report in results:
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
