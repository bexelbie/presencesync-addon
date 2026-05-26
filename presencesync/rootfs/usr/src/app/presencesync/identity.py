# ABOUTME: Stores stable hashed device identities in /data for consistent device tracking.
# ABOUTME: Resolves per-device addon overrides by matching persisted 12-character hashes.
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

from . import state

log = logging.getLogger(__name__)

_DEVICE_KEYS = ("stationary_radius", "exclude", "unavailable_timeout")


def hash_device_id(raw_id: str) -> str:
    """Return a stable 12-character hash for a device identifier."""
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]


class DeviceIdentityStore:
    def __init__(self, path: Path | None = None):
        self._path = path or (state.DATA_DIR / "device_identity.json")
        self._lock = threading.RLock()
        self._data = {"devices": {}}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            log.exception("Failed to load device identities from %s", self._path)
            return

        devices = data.get("devices")
        if isinstance(devices, dict):
            self._data = {"devices": devices}
            return

        log.warning("Ignoring invalid device identities in %s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(f"{self._path}.tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp_path.replace(self._path)

    @staticmethod
    def _device_entries(addon_config) -> list[dict]:
        if isinstance(addon_config, list):
            return [entry for entry in addon_config if isinstance(entry, dict)]
        if isinstance(addon_config, dict):
            devices = addon_config.get("devices")
            if isinstance(devices, list):
                return [entry for entry in devices if isinstance(entry, dict)]
            if "id" in addon_config:
                return [addon_config]
        return []

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit - 3]}..."

    def register(self, raw_id: str, name: str, source: str) -> str:
        device_hash = hash_device_id(raw_id)
        entry = {"raw_id": raw_id, "name": name, "source": source}
        with self._lock:
            devices = self._data.setdefault("devices", {})
            if devices.get(device_hash) != entry:
                devices[device_hash] = entry
                self._save()
        return device_hash

    def get_hash(self, raw_id: str) -> str:
        return hash_device_id(raw_id)

    def known_device_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._data.get("devices", {}).keys())

    def get_config(self, device_hash: str, addon_config) -> dict:
        for entry in self._device_entries(addon_config):
            if entry.get("id") != device_hash:
                continue
            return {
                key: entry[key]
                for key in _DEVICE_KEYS
                if key in entry and entry[key] is not None
            }
        return {}

    def is_excluded(self, device_hash: str, addon_config) -> bool:
        return bool(self.get_config(device_hash, addon_config).get("exclude", False))

    def get_stationary_radius(self, device_hash: str, addon_config, global_default: int) -> int:
        value = self.get_config(device_hash, addon_config).get("stationary_radius")
        return global_default if value is None else int(value)

    def get_unavailable_timeout(self, device_hash: str, addon_config, global_default: int) -> int:
        value = self.get_config(device_hash, addon_config).get("unavailable_timeout")
        return global_default if value is None else int(value)

    def log_device_table(self) -> None:
        with self._lock:
            devices = dict(self._data.get("devices", {}))

        lines = [
            "Known devices:",
            f"{'hash':12}  {'name':20}  {'source':11}  raw_id",
            f"{'-' * 12}  {'-' * 20}  {'-' * 11}  {'-' * 24}",
        ]
        for device_hash in sorted(devices):
            entry = devices[device_hash]
            lines.append(
                f"{device_hash:12}  "
                f"{self._truncate(str(entry.get('name', '')), 20):20}  "
                f"{self._truncate(str(entry.get('source', '')), 11):11}  "
                f"{self._truncate(str(entry.get('raw_id', '')), 24)}"
            )
        log.info("\n%s", "\n".join(lines))


_store: DeviceIdentityStore | None = None


def get() -> DeviceIdentityStore:
    global _store
    if _store is None:
        _store = DeviceIdentityStore()
    return _store
