"""Persistent state — survives container restarts via /data volume."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("PRESENCESYNC_DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "presencesync.json"
APPLE_STATE_PATH = DATA_DIR / "apple_state.pickle"
APPLE_STATE_PATH_LEGACY = DATA_DIR / "apple_state.json"  # historical JSON file
BUNDLE_DIR = DATA_DIR / "bundle"  # extracted bundle contents


@dataclass
class HomeLocation:
    latitude: float = 0.0
    longitude: float = 0.0
    radius_m: int = 100


@dataclass
class MqttConfig:
    host: str = "core-mosquitto"
    port: int = 1883
    username: str = ""
    password: str = ""
    discovery_prefix: str = "homeassistant"
    state_prefix: str = "presencesync"


@dataclass
class AppleConfig:
    username: str = ""
    password: str = ""           # stored in clear text on the add-on's /data volume; HA-controlled
    anisette_url: str = ""


@dataclass
class TrackingConfig:
    poll_interval_s: int = 60
    include_audio_accessories: bool = False  # AirPods etc
    include_devices: bool = True             # iPhone / iPad / Mac / Watch
    include_airtags: bool = True
    ignored_identifiers: list[str] = field(default_factory=list)


@dataclass
class Settings:
    apple: AppleConfig = field(default_factory=AppleConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    home: HomeLocation = field(default_factory=HomeLocation)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    bundle_uploaded: bool = False  # set True once user uploads a presencesync-bundle.tar.gz

    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            return cls()
        raw = json.loads(CONFIG_PATH.read_text())
        return cls(
            apple=AppleConfig(**raw.get("apple", {})),
            mqtt=MqttConfig(**raw.get("mqtt", {})),
            home=HomeLocation(**raw.get("home", {})),
            tracking=TrackingConfig(**raw.get("tracking", {})),
            bundle_uploaded=raw.get("bundle_uploaded", False),
        )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(CONFIG_PATH)


# Single in-memory copy plus a write lock
_settings = Settings.load()
_lock = asyncio.Lock()


def get() -> Settings:
    return _settings


async def update(mutator) -> Settings:
    async with _lock:
        mutator(_settings)
        _settings.save()
    return _settings


# --- Apple auth state ---------------------------------------------------
# AsyncAppleAccount.__getstate__() returns a nested dict with bytes, datetimes,
# and (for some anisette providers) closure-bound callables. JSON with
# `default=str` silently converts those to strings on save — and on restore
# they're no longer reconstructable, so the saved state always fails to load
# and we end up re-prompting for 2FA every restart. Use pickle, which
# roundtrips everything cleanly.

def save_apple_state(state: object) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    import pickle
    tmp = APPLE_STATE_PATH.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(state))
    tmp.replace(APPLE_STATE_PATH)
    # remove any stale JSON copy from older versions
    APPLE_STATE_PATH_LEGACY.unlink(missing_ok=True)


def load_apple_state():
    import pickle
    if APPLE_STATE_PATH.exists():
        try:
            return pickle.loads(APPLE_STATE_PATH.read_bytes())
        except Exception as err:
            log.warning("apple_state.pickle exists but won't load (%s); clearing", err)
            APPLE_STATE_PATH.unlink(missing_ok=True)
    # Fallback to old JSON file (one-time migration: probably broken but try)
    if APPLE_STATE_PATH_LEGACY.exists():
        try:
            data = json.loads(APPLE_STATE_PATH_LEGACY.read_text())
            APPLE_STATE_PATH_LEGACY.unlink(missing_ok=True)
            return data
        except Exception:
            APPLE_STATE_PATH_LEGACY.unlink(missing_ok=True)
    return None


def clear_apple_state() -> None:
    APPLE_STATE_PATH.unlink(missing_ok=True)
    APPLE_STATE_PATH_LEGACY.unlink(missing_ok=True)
