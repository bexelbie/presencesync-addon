"""Persistent state — survives container restarts via /data volume."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger("presencesync.state")

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
# We save the AccountStateMapping subset of AsyncAppleAccount.__getstate__()
# rather than the full __dict__: the full dict contains a reference to the
# running uvloop event loop, which can't be pickled (uvloop.Loop has a
# non-trivial __cinit__ and refuses pickle protocol). The subset is enough
# for AsyncAppleAccount(state_info=...) to reconstruct the session.

_APPLE_STATE_KEEP_KEYS = ("type", "ids", "account", "login", "anisette")


def save_apple_state(state: object) -> None:
    import pickle
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = state
    # If we got a dict, filter to AccountStateMapping schema keys first.
    if isinstance(state, dict):
        all_keys = sorted(state.keys())
        payload = {k: v for k, v in state.items() if k in _APPLE_STATE_KEEP_KEYS}
        log.info("apple_state: keeping %d/%d top-level keys (%s); ALL keys present: %s",
                 len(payload), len(state), sorted(payload.keys()), all_keys)
        # If the AccountStateMapping filter caught nothing, the dict is probably
        # the full __dict__ (with leading-underscore field names) rather than
        # the schema'd export. Try pickling the whole thing and let the per-key
        # fallback drop the unpicklable uvloop bits.
        if not payload:
            payload = state
    try:
        data = pickle.dumps(payload)
    except (TypeError, ValueError) as err:
        log.warning("apple_state: pickle failed (%s) — saving per-key best-effort", err)
        # Last-resort: try to pickle each value individually, drop the ones
        # that fail. This usually keeps the auth tokens while dropping any
        # cached aiohttp sessions or loop references that snuck in.
        if isinstance(payload, dict):
            cleaned = {}
            for k, v in payload.items():
                try:
                    pickle.dumps(v)
                    cleaned[k] = v
                except Exception:
                    log.debug("apple_state: skipping unpicklable key %s", k)
            try:
                data = pickle.dumps(cleaned)
                log.info("apple_state: salvaged %d keys after filter", len(cleaned))
            except Exception as err2:
                log.warning("apple_state: even per-key filter failed: %s", err2)
                return
        else:
            return
    tmp = APPLE_STATE_PATH.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(APPLE_STATE_PATH)
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
