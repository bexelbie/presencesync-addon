"""Decrypt OwnedBeacons/*.record files using the extracted BeaconStore key."""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@dataclass(frozen=True)
class OwnedBeacon:
    """Parsed contents of one OwnedBeacons/*.record file."""
    identifier: str         # UUID matching the record filename stem
    private_key: bytes      # primary master key (P-224)
    shared_secret: bytes    # secondary master key
    name: str | None        # human-readable name, may be None
    model: str | None       # AirTag, iPhone17,2, etc.
    raw: dict               # the full decrypted plist for anything else we need


def decrypt_record(blob: bytes, beaconstore_key: bytes) -> dict:
    """Decrypt one .record file. Outer plist is [nonce, tag, ciphertext]; AES-GCM."""
    outer = plistlib.loads(blob)
    if not isinstance(outer, list) or len(outer) < 3:
        raise ValueError("record outer plist is not a 3-element list")
    nonce, tag, ct = outer[0], outer[1], outer[2]
    cipher = Cipher(algorithms.AES(beaconstore_key), modes.GCM(nonce, tag))
    d = cipher.decryptor()
    pt = d.update(ct) + d.finalize()
    inner = plistlib.loads(pt)
    if not isinstance(inner, dict):
        raise ValueError("decrypted plist isn't a dict")
    return inner


def load_bundle(bundle_dir: Path) -> tuple[bytes, list[OwnedBeacon]]:
    """Read BeaconStore.key + every record under OwnedBeacons/ in the bundle."""
    bs_path = bundle_dir / "BeaconStore.key"
    if not bs_path.exists():
        raise FileNotFoundError(
            f"BeaconStore.key missing in bundle. "
            f"Bundle dir contents: {[p.name for p in bundle_dir.iterdir()]}"
        )
    key = bs_path.read_bytes()
    if len(key) != 32:
        raise ValueError(f"BeaconStore.key is {len(key)} bytes, expected 32")

    beacons_dir = bundle_dir / "OwnedBeacons"
    if not beacons_dir.is_dir():
        raise FileNotFoundError(
            f"OwnedBeacons/ directory missing. "
            f"Bundle dir contents: {[p.name for p in bundle_dir.iterdir()]}"
        )

    records = sorted(beacons_dir.glob("*.record"))
    if not records:
        raise ValueError(
            f"OwnedBeacons/ has no .record files. "
            f"Contains: {[p.name for p in beacons_dir.iterdir()][:10]}"
        )

    out: list[OwnedBeacon] = []
    for rec in records:
        try:
            blob = rec.read_bytes()
            inner = decrypt_record(blob, key)
        except Exception as err:
            raise ValueError(
                f"failed to decrypt {rec.name} ({len(blob) if 'blob' in dir() else '?'}B): "
                f"{type(err).__name__}: {err}. First bytes: {blob[:24].hex() if 'blob' in dir() else 'n/a'}"
            ) from err
        private_key = inner.get("privateKey", {}).get("key", {}).get("data") or inner.get("privateKey")
        shared_secret = inner.get("sharedSecret", {}).get("key", {}).get("data") or inner.get("sharedSecret")
        if isinstance(private_key, dict):
            private_key = private_key.get("data")
        if isinstance(shared_secret, dict):
            shared_secret = shared_secret.get("data")
        if not isinstance(private_key, (bytes, bytearray)):
            continue
        out.append(OwnedBeacon(
            identifier=rec.stem,
            private_key=bytes(private_key),
            shared_secret=bytes(shared_secret) if isinstance(shared_secret, (bytes, bytearray)) else b"",
            name=inner.get("name"),
            model=inner.get("model") or inner.get("productId"),
            raw=inner,
        ))
    return key, out
