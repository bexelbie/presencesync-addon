# ABOUTME: Handles Apple account login state and fetches owned and shared Find My locations.
# ABOUTME: Loads exported accessory keys and talks to Apple through the local anisette service.
"""Wrapper around findmy.py — login, 2FA, and AirTag location fetching.

Handles both owned (via FindMy.py master_key rotation) and shared (via
custom sharedFetch API with HKDF bundle keys) accessories.

Uses RemoteAnisetteProvider pointing at the embedded anisette-v3-server.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import plistlib
import re
import time
from dataclasses import dataclass, field
from datetime import timezone
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
ALIGNMENT_FILE = state.DATA_DIR / "alignment.json"


def _load_shared_metadata() -> dict:
    """Load metadata.json and build share_id → {owner_handle, share_date} map."""
    meta_path = KEYS_DIR / "metadata.json"
    if not meta_path.exists():
        return {}
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return {}
    # Map: sharing_circle_id → owner_handle from shared_beacons via member_circles
    info: dict[str, dict] = {}
    for beacon in meta.get("shared_beacons", {}).values():
        circle_id = beacon.get("sharing_circle_id", "")
        owner = beacon.get("owner_handle", "").removeprefix("mailto:")
        if circle_id and owner:
            info[circle_id] = {"owner": owner}
    return info


def _is_owned_plist(path: Path) -> bool:
    """Check if a plist is an owned AirTag/accessory (has privateKey but no wildRootKey)."""
    try:
        content = path.read_text()
        return "<key>privateKey</key>" in content and "<key>wildRootKey</key>" not in content
    except Exception:
        return False


def _load_alignment() -> dict[str, dict]:
    """Load saved alignment data keyed by accessory identifier."""
    if not ALIGNMENT_FILE.exists():
        return {}
    try:
        return json.loads(ALIGNMENT_FILE.read_text())
    except Exception:
        return {}


def _save_alignment(accessories: list[FindMyAccessory]) -> None:
    """Persist alignment_date + alignment_index for each accessory.

    If a device has never been aligned (still at pairing date / index 0),
    advance alignment to now so future scans only cover 7 days.
    """
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    data: dict[str, dict] = {}
    for acc in accessories:
        ident = getattr(acc, "_identifier", None) or getattr(acc, "identifier", None)
        if not ident:
            continue
        a_date = getattr(acc, "_alignment_date", None)
        a_index = getattr(acc, "_alignment_index", None)
        paired_at = getattr(acc, "_paired_at", None)
        if a_date is not None and a_index is not None:
            # If alignment never advanced beyond pairing defaults, cap to now
            if paired_at and a_date == paired_at and a_index == 0:
                a_date = now
                a_index = 0
                log.info("Advancing alignment for %s (never reported) to now", ident)
            data[ident] = {
                "alignment_date": a_date.isoformat(),
                "alignment_index": a_index,
            }
    try:
        ALIGNMENT_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        log.warning("Failed to save alignment data")


def _load_owned_plist(path: Path, alignment: dict | None = None) -> FindMyAccessory | None:
    """Load an owned AirTag from export-findmy's flat plist format.

    export-findmy produces plists with nanosecond pairingDate values
    that Python's plistlib can't parse, and a flat structure (no nested
    dicts like macOS's native format). We strip fractional seconds and
    construct FindMyAccessory directly.
    """
    try:
        text = path.read_text()
        # Strip fractional seconds from <date> elements (plistlib only handles YYYY-MM-DDTHH:MM:SSZ)
        fixed = re.sub(
            r"<date>(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+Z</date>",
            r"<date>\1Z</date>",
            text,
        )
        data = plistlib.loads(fixed.encode())

        private_key = data["privateKey"]
        # P-224 key: 57B public + 28B private scalar = 85B
        # P-256 key: 65B public + 32B private scalar = 97B
        # Take last 28 bytes as master_key (matches FindMy.py convention)
        master_key = private_key[-28:]

        # Restore alignment if available
        from datetime import datetime
        alignment_date = None
        alignment_index = None
        if alignment:
            try:
                alignment_date = datetime.fromisoformat(alignment["alignment_date"])
                alignment_index = alignment["alignment_index"]
            except (KeyError, ValueError):
                pass

        acc = FindMyAccessory(
            master_key=master_key,
            skn=data["sharedSecret"],
            sks=data.get("secondarySharedSecret", data.get("secureLocationsSharedSecret", b"")),
            paired_at=data["pairingDate"].replace(tzinfo=timezone.utc),
            name=data.get("name") or None,
            model=data.get("model") or None,
            identifier=data.get("identifier") or None,
            alignment_date=alignment_date,
            alignment_index=alignment_index,
        )
        return acc
    except Exception as e:
        log.warning("Failed to load owned plist %s: %s", path.name, e)
        return None


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
    shared_by: str | None = None
    shared_date: str | None = None


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
                        # Convert raw int back to LoginState enum
                        if k == "_login_state" and isinstance(v, int):
                            v = LoginState(v)
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
        - Owned accessories (flat plists from export-findmy, or encrypted + BeaconStore.key)
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

        # Load owned accessories
        bs_path = bundle_dir / "BeaconStore.key"
        if bs_path.exists():
            # macOS encrypted format: decrypt with BeaconStore.key
            self.beaconstore_key = bs_path.read_bytes()
            if len(self.beaconstore_key) != 32:
                log.warning("BeaconStore.key is %dB, expected 32 — skipping owned accessories",
                            len(self.beaconstore_key))
            else:
                for p in bundle_dir.rglob("._*"):
                    if p.is_file():
                        p.unlink(missing_ok=True)
                _fm_plist._DEFAULT_SEARCH_PATH = bundle_dir
                self.accessories = list_accessories(key=self.beaconstore_key, search_path=bundle_dir)
        else:
            # export-findmy flat format: load directly (no BeaconStore.key needed)
            alignment_data = _load_alignment()
            for plist_path in bundle_dir.glob("*.plist"):
                if _is_owned_plist(plist_path):
                    # Peek at identifier to find alignment (need to parse plist briefly)
                    try:
                        text = plist_path.read_text()
                        fixed = re.sub(
                            r"<date>(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+Z</date>",
                            r"<date>\1Z</date>", text)
                        ident = plistlib.loads(fixed.encode()).get("identifier")
                    except Exception:
                        ident = None
                    align = alignment_data.get(ident) if ident else None
                    acc = _load_owned_plist(plist_path, align)
                    if acc:
                        self.accessories.append(acc)
                        log.info("Loaded owned accessory: %s (%s)%s",
                                 acc.name, acc.identifier,
                                 " [aligned]" if align else "")

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

        # Save alignment data so subsequent restarts don't rescan from pairing date
        _save_alignment(self.accessories)

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

        # Get anisette headers from the account (uses uid/devid internally)
        try:
            anisette_headers = await self.account.get_anisette_headers()
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
        # Build lookup: accessory name → shared metadata
        shared_meta = _load_shared_metadata()
        acc_by_name = {acc.name: acc for acc in self.shared_accessories}

        out: list[LocationFix] = []
        for name, reports in results.items():
            if not reports:
                continue
            latest = max(reports, key=lambda r: r.timestamp)
            acc = acc_by_name.get(name)
            owner = None
            share_date_str = None
            if acc:
                meta_info = shared_meta.get(acc.share_id, {})
                owner = meta_info.get("owner")
                if acc.share_date:
                    share_date_str = acc.share_date.strftime("%Y-%m-%d")
            out.append(LocationFix(
                identifier=acc.share_id,
                name=name,
                model="AirTag (Shared)",
                latitude=latest.latitude,
                longitude=latest.longitude,
                horizontal_accuracy=float(latest.accuracy),
                timestamp_unix=int(latest.timestamp.timestamp()),
                shared_by=owner,
                shared_date=share_date_str,
            ))
        log.info("fetch_shared: %d/%d shared accessories reported",
                 len(out), len(self.shared_accessories))
        return out

    def _persist(self) -> None:
        if self.account is None:
            return
        try:
            # Save only the restorable fields (avoid pickling anisette/aiohttp objects)
            blob = {}
            for attr in ("_uid", "_devid", "_username", "_password",
                         "_login_state", "_login_state_data", "_account_info"):
                val = getattr(self.account, attr, None)
                if val is not None:
                    # Convert LoginState enum to int for safe pickling
                    if attr == "_login_state" and hasattr(val, "value"):
                        val = val.value
                    blob[attr] = val
            if blob:
                state.save_apple_state(blob)
        except Exception:
            log.warning("Could not persist Apple state", exc_info=True)
