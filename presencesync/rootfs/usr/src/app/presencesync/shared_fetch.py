# ABOUTME: Fetches and decrypts shared Find My accessory reports from Apple's sharedFetch APIs.
# ABOUTME: Derives shared bundle keys, signs requests, and parses decrypted location payloads.
"""Shared AirTag location fetching via Apple's sharedFetch API.

This module handles the complete flow for fetching locations of shared
Find My accessories:
1. Call getShare API (ECDSA-signed) to get base_date alignment
2. Calculate daily bundle indices from base_date
3. Generate HKDF-derived bundle decryption keys
4. Call /findmyservice/v2/fetch with sharedFetch payload
5. Decrypt locDecryptKey per payload (AES-256-GCM)
6. Decrypt individual location reports (P-224 ECDH + AES-128-GCM)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import plistlib
import struct
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

log = logging.getLogger(__name__)

APPLE_EPOCH_OFFSET = 978307200  # seconds between Unix epoch and Apple epoch (2001-01-01)
FETCH_URL = "https://gateway.icloud.com/findmyservice/v2/fetch"
GETSHARE_URL = "https://gateway.icloud.com/findmyservice/itemsharing/getShare"


@dataclass
class SharedLocationReport:
    """A decrypted location report for a shared accessory."""
    latitude: float
    longitude: float
    accuracy: int
    confidence: int
    timestamp: datetime


@dataclass
class SharedAccessory:
    """Parsed shared accessory from exported plist."""
    name: str
    share_id: str  # sharingCircle identifier
    private_key: bytes  # 32-byte P-256 private key (from joinToken)
    wild_root_key: bytes  # 32-byte HKDF root key
    circle_shared_secret: bytes  # 32-byte AES-256-GCM key
    circle_owner: str  # owner member ID for getShare
    model: str | None = None
    share_date: datetime | None = None

    @classmethod
    def from_plist(cls, path: Path) -> SharedAccessory | None:
        """Load a shared accessory from an exported plist file."""
        try:
            with open(path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            # Try text-mode for XML plists with problematic dates
            try:
                with open(path, "r") as f:
                    content = f.read()
                import re
                content = re.sub(
                    r"<date>(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+Z</date>",
                    r"<date>\1Z</date>", content
                )
                data = plistlib.loads(content.encode())
            except Exception as e:
                log.warning("Failed to load plist %s: %s", path, e)
                return None

        # Check if this is a shared plist
        if "privateKey" not in data or "wildRootKey" not in data:
            return None
        if "circleSharedSecret" not in data or "sharingCircle" not in data:
            return None

        private_key = bytes(data["privateKey"])
        if len(private_key) != 32:
            log.warning("Shared plist %s: privateKey is %d bytes, expected 32", path, len(private_key))
            return None

        share_date = None
        if "shareDate" in data:
            sd = data["shareDate"]
            if isinstance(sd, datetime):
                share_date = sd

        return cls(
            name=data.get("name", path.stem),
            share_id=data["sharingCircle"],
            private_key=private_key,
            wild_root_key=bytes(data["wildRootKey"]),
            circle_shared_secret=bytes(data["circleSharedSecret"]),
            circle_owner=data.get("circleOwner", ""),
            model=data.get("model"),
            share_date=share_date,
        )


def is_shared_plist(path: Path) -> bool:
    """Quick check if a plist file is a shared accessory."""
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
        return "privateKey" in data and "wildRootKey" in data
    except Exception:
        # Fallback: check XML text for key presence (handles nanosecond dates)
        try:
            content = path.read_text()
            return "<key>privateKey</key>" in content and "<key>wildRootKey</key>" in content
        except Exception:
            return False


# --- Crypto helpers ---

def _hkdf_sha256(key_material: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 extract-then-expand (no salt)."""
    prk = hmac.HMAC(b"\x00" * 32, key_material, hashlib.sha256).digest()
    t = hmac.HMAC(prk, info + b"\x01", hashlib.sha256).digest()
    return t[:length]


def _wild_root_key_idx(root_key: bytes, idx: int) -> bytes:
    """Derive bundle decryption key from WildRootKey at day index."""
    return _hkdf_sha256(root_key, str(idx).encode())


def _get_bundle_data(root_key: bytes, idx: int) -> dict:
    """Generate shareBundles entry for Apple's fetch API."""
    key = _wild_root_key_idx(root_key, idx)
    return {
        "startIndex": (idx - 1) * 96,
        "endIndex": (idx * 96) - 1,
        "bundleIndex": idx,
        "bundleDecryptionKey": base64.b64encode(key).decode(),
    }


def _member_token_from_private_key(private_key_bytes: bytes) -> bytes:
    """Compute member token: 0x02 + X coordinate of P-256 public key."""
    priv_int = int.from_bytes(private_key_bytes, "big")
    priv_key = ec.derive_private_key(priv_int, ec.SECP256R1(), default_backend())
    pub_numbers = priv_key.public_key().public_numbers()
    x_bytes = pub_numbers.x.to_bytes(32, "big")
    return b"\x02" + x_bytes


def _decrypt_circle_secret(circle_shared_secret: bytes, ciphertext: bytes) -> bytes:
    """Decrypt locDecryptKey using CircleSecretKey (AES-256-GCM).
    Ciphertext is a plist array: [nonce, tag, data]."""
    decoded = plistlib.loads(ciphertext)
    nonce = bytes(decoded[0])
    tag = bytes(decoded[1])
    data = bytes(decoded[2])
    aesgcm = AESGCM(circle_shared_secret)
    return aesgcm.decrypt(nonce, data + tag, b"")


def _decrypt_location_report(priv_key_bytes: bytes, encrypted_payload: bytes) -> SharedLocationReport | None:
    """Decrypt a single FindMy location report."""
    if len(encrypted_payload) < 57:
        return None

    # Parse header (two formats: 88 bytes or 89 bytes)
    if len(encrypted_payload) == 88:
        timestamp_raw = int.from_bytes(encrypted_payload[:4], "big")
        confidence = encrypted_payload[4]
        encrypted_data = encrypted_payload[5:]
    else:
        timestamp_raw = int.from_bytes(encrypted_payload[:4], "big")
        confidence = encrypted_payload[5]
        encrypted_data = encrypted_payload[6:]

    timestamp = datetime.fromtimestamp(
        APPLE_EPOCH_OFFSET + timestamp_raw, tz=timezone.utc
    )

    eph_pub_bytes = encrypted_data[:57]
    ciphertext_with_tag = encrypted_data[57:]

    try:
        priv_int = int.from_bytes(priv_key_bytes, "big")
        priv_key = ec.derive_private_key(priv_int, ec.SECP224R1(), default_backend())

        eph_pub = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP224R1(), eph_pub_bytes
        )

        shared_key = priv_key.exchange(ec.ECDH(), eph_pub)

        symmetric = hashlib.sha256(
            shared_key + b"\x00\x00\x00\x01" + eph_pub_bytes
        ).digest()

        aes_key = symmetric[:16]
        nonce = symmetric[16:]  # 16-byte nonce (non-standard)

        aesgcm = AESGCM(aes_key)
        decrypted = aesgcm.decrypt(nonce, ciphertext_with_tag, None)

        lat = struct.unpack(">i", decrypted[0:4])[0] / 10_000_000
        lon = struct.unpack(">i", decrypted[4:8])[0] / 10_000_000
        accuracy = decrypted[8] if len(decrypted) > 8 else 0

        return SharedLocationReport(
            latitude=lat,
            longitude=lon,
            accuracy=accuracy,
            confidence=confidence,
            timestamp=timestamp,
        )
    except Exception as e:
        log.debug("Failed to decrypt report: %s", e)
        return None


# --- API calls ---

def _get_share_base_date(
    dsid: str, token: str, anisette_headers: dict,
    share_id: str, owner_id: str, private_key_bytes: bytes
) -> int | None:
    """Call getShare API to retrieve the base_date for key alignment.
    Returns base_date as milliseconds since UNIX epoch, or None on failure."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    body = json.dumps({
        "timestamp": timestamp_ms,
        "type": "item",
        "shareId": share_id,
        "memberId": owner_id,
        "packages": [
            {"maxKeys": 300, "startIndex": 0, "metadata": False, "type": "primaryAddress"},
            {"maxKeys": 300, "startIndex": 0, "metadata": False, "type": "beaconAttributes"},
        ]
    })

    # Sign the request body with the joinToken private key (ECDSA P-256 SHA-256)
    priv_int = int.from_bytes(private_key_bytes, "big")
    priv_key = ec.derive_private_key(priv_int, ec.SECP256R1(), default_backend())
    signature = priv_key.sign(body.encode(), ec.ECDSA(hashes.SHA256()))

    headers = dict(anisette_headers)
    headers["Content-Type"] = "application/json"
    headers["x-apple-share-auth"] = base64.b64encode(signature).decode()

    resp = requests.post(
        GETSHARE_URL,
        data=body,
        headers=headers,
        auth=(dsid, token),
        timeout=30,
    )

    if resp.status_code != 200:
        log.warning("getShare failed: %d %s", resp.status_code, resp.text[:200])
        return None

    result = resp.json()
    key_packages = result.get("keyPackages", [])

    for pkg in key_packages:
        if pkg.get("type") == "primaryAddress":
            alignment = pkg.get("alignment", {})
            base_date = alignment.get("baseDate")
            if base_date:
                log.debug("getShare base_date=%d (primaryAddress)", base_date)
                return base_date

    log.warning("getShare: no primaryAddress base_date found")
    return None


def fetch_shared_locations(
    dsid: str,
    search_party_token: str,
    anisette_headers: dict,
    accessories: list[SharedAccessory],
) -> dict[str, list[SharedLocationReport]]:
    """Fetch locations for shared accessories.

    Args:
        dsid: Apple DSID (from login)
        search_party_token: searchPartyToken from MobileMe delegate
        anisette_headers: dict of anisette headers (from RemoteAnisetteProvider or GET /)
        accessories: list of SharedAccessory objects

    Returns:
        dict mapping accessory name → list of SharedLocationReport
    """
    if not accessories:
        return {}

    shared_search = []
    plist_info: dict[str, SharedAccessory] = {}  # shareId → accessory

    for acc in accessories:
        # Get base_date from getShare API
        base_date_ms = _get_share_base_date(
            dsid, search_party_token, anisette_headers,
            acc.share_id, acc.circle_owner, acc.private_key
        )

        if base_date_ms:
            base_date = datetime.fromtimestamp(base_date_ms / 1000, tz=timezone.utc)
            diff = datetime.now(timezone.utc) - base_date
            days_elapsed = int((diff.total_seconds() + 43200) // 86400)
        else:
            days_elapsed = 500  # fallback

        # Request current day + 1 extra (day boundaries align to base_date time, not midnight)
        search_range = range(days_elapsed - 6, days_elapsed + 2)

        member_token = _member_token_from_private_key(acc.private_key)
        share_bundles = [_get_bundle_data(acc.wild_root_key, idx) for idx in search_range]

        shared_search.append({
            "shareId": acc.share_id,
            "type": "item",
            "memberToken": base64.b64encode(member_token).decode(),
            "shareBundles": share_bundles,
            "ownedDeviceIds": [],
        })

        plist_info[acc.share_id] = acc
        log.info("Shared %s: days_elapsed=%d, bundles=[%d..%d]",
                 acc.name, days_elapsed, search_range.start, search_range.stop - 1)

    # Build request payload
    payload = {
        "clientContext": {
            "clientBundleIdentifier": "com.apple.icloud.searchpartyuseragent",
            "policy": "foregroundClient",
        },
        "sharedFetch": shared_search,
        "fetch": [],
    }

    headers = dict(anisette_headers)

    resp = requests.post(
        FETCH_URL,
        json=payload,
        headers=headers,
        auth=(str(dsid), search_party_token),
        timeout=60,
    )

    if resp.status_code != 200:
        log.error("sharedFetch failed: %d %s", resp.status_code, resp.text[:500])
        return {}

    result = resp.json()
    acsn = result.get("acsnLocations", {})
    location_payloads = acsn.get("locationPayload", [])
    log.info("sharedFetch: status=%s, payloads=%d", acsn.get("statusCode"), len(location_payloads))

    # Process each payload
    results: dict[str, list[SharedLocationReport]] = {}
    for payload_item in location_payloads:
        share_id = payload_item.get("shareId")
        loc_decrypt_key_b64 = payload_item.get("locDecryptKey")
        location_infos = payload_item.get("locationInfo", [])

        if not share_id or share_id not in plist_info:
            continue

        acc = plist_info[share_id]

        if not loc_decrypt_key_b64:
            continue

        # Decrypt the locDecryptKey with circleSharedSecret
        try:
            loc_key_ciphertext = base64.b64decode(loc_decrypt_key_b64)
            decrypted_key = _decrypt_circle_secret(acc.circle_shared_secret, loc_key_ciphertext)
            if len(decrypted_key) < 85:
                log.warning("%s: decrypted key too short (%d bytes)", acc.name, len(decrypted_key))
                continue
            p224_priv_key = decrypted_key[57:]  # 28-byte P-224 private key
        except Exception as e:
            log.warning("%s: failed to decrypt locDecryptKey: %s", acc.name, e)
            continue

        # Decrypt each location report
        reports = []
        for loc_b64 in location_infos:
            encrypted = base64.b64decode(loc_b64)
            report = _decrypt_location_report(p224_priv_key, encrypted)
            if report:
                reports.append(report)

        if acc.name not in results:
            results[acc.name] = []
        results[acc.name].extend(reports)

    for name, reports in results.items():
        log.info("Shared %s: %d reports decrypted", name, len(reports))

    return results
