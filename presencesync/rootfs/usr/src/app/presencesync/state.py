"""Persistent state — survives container restarts via /data volume."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger("presencesync.state")

DATA_DIR = Path(os.environ.get("PRESENCESYNC_DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "presencesync.json"
APPLE_STATE_PATH = DATA_DIR / "apple_state.pickle"
BUNDLE_DIR = DATA_DIR / "bundle"
KEYS_DIR = DATA_DIR / "keys"
DEVICE_IDENTITY_PATH = DATA_DIR / "device_identity.json"


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
    password: str = ""
    anisette_url: str = ""  # empty = use built-in LocalAnisetteProvider


@dataclass
class TrackingConfig:
    poll_interval_s: int = 60
    include_devices: bool = True
    include_airtags: bool = True

    # Smart polling (global — pyicloud fetches all devices at once)
    dynamic_polling: bool = True
    flap_suppression_count: int = 3
    stationary_threshold_minutes: int = 15
    battery_low_threshold: float = 0.20
    battery_critical_threshold: float = 0.10
    stale_threshold_hours: float = 4.0

    # AirTag polling
    airtag_poll_interval_s: int = 600
    airtag_movement_interval_s: int = 300
    airtag_movement_threshold_m: float = 200.0


@dataclass
class Settings:
    apple: AppleConfig = field(default_factory=AppleConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    home: HomeLocation = field(default_factory=HomeLocation)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    bundle_uploaded: bool = False

    @classmethod
    def load(cls) -> "Settings":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load config (%s), using defaults", e)
            return cls()
        return cls(
            apple=AppleConfig(**{k: v for k, v in raw.get("apple", {}).items() if k in AppleConfig.__dataclass_fields__}),
            mqtt=MqttConfig(**{k: v for k, v in raw.get("mqtt", {}).items() if k in MqttConfig.__dataclass_fields__}),
            home=HomeLocation(**{k: v for k, v in raw.get("home", {}).items() if k in HomeLocation.__dataclass_fields__}),
            tracking=TrackingConfig(**{k: v for k, v in raw.get("tracking", {}).items() if k in TrackingConfig.__dataclass_fields__}),
            bundle_uploaded=raw.get("bundle_uploaded", False),
        )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(CONFIG_PATH)


_settings = Settings.load()
_lock = asyncio.Lock()


def get() -> Settings:
    return _settings


async def update(mutator) -> Settings:
    async with _lock:
        mutator(_settings)
        _settings.save()
    return _settings


# --- Apple auth state persistence ---

def save_apple_state(state_data: object) -> None:
    import pickle
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = pickle.dumps(state_data)
    except (TypeError, ValueError) as err:
        log.warning("apple_state: pickle failed (%s)", err)
        if isinstance(state_data, dict):
            cleaned = {}
            for k, v in state_data.items():
                try:
                    pickle.dumps(v)
                    cleaned[k] = v
                except Exception:
                    pass
            try:
                data = pickle.dumps(cleaned)
            except Exception:
                return
        else:
            return
    tmp = APPLE_STATE_PATH.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(APPLE_STATE_PATH)


def load_apple_state():
    import pickle
    if APPLE_STATE_PATH.exists():
        try:
            return pickle.loads(APPLE_STATE_PATH.read_bytes())
        except Exception as err:
            log.warning("apple_state.pickle corrupt (%s); clearing", err)
            APPLE_STATE_PATH.unlink(missing_ok=True)
    return None


def clear_apple_state() -> None:
    APPLE_STATE_PATH.unlink(missing_ok=True)
