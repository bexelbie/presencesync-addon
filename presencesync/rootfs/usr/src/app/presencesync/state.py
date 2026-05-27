# ABOUTME: Persistent state and configuration — survives container restarts via /data volume.
# ABOUTME: Loads add-on options from HA, persists Apple auth state and runtime data.
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
OPTIONS_PATH = DATA_DIR / "options.json"


# ── Add-on config (from HA options UI → /data/options.json) ──────────────────

@dataclass
class AddonConfig:
    """Configuration from HA add-on options. Read-only at runtime."""
    log_level: str = "info"
    poll_interval: int = 300
    refresh_interval: int = 1800
    item_poll_interval: int = 600
    stationary_radius: int = 50
    devices: list = field(default_factory=list)

    @classmethod
    def load(cls) -> "AddonConfig":
        """Load from /data/options.json (written by HA Supervisor)."""
        if not OPTIONS_PATH.exists():
            log.warning("No options.json found, using defaults")
            return cls()
        try:
            raw = json.loads(OPTIONS_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load options.json (%s), using defaults", e)
            return cls()
        return cls(
            log_level=raw.get("log_level", "info"),
            poll_interval=raw.get("poll_interval", 300),
            refresh_interval=raw.get("refresh_interval", 1800),
            item_poll_interval=raw.get("item_poll_interval", 600),
            stationary_radius=raw.get("stationary_radius", 50),
            devices=raw.get("devices", []),
        )


# ── Persisted runtime state (Apple creds + flags) ────────────────────────────

@dataclass
class MqttConfig:
    """MQTT broker connection details (auto-discovered from Supervisor)."""
    host: str = "core-mosquitto"
    port: int = 1883
    username: str = ""
    password: str = ""


@dataclass
class AppleConfig:
    username: str = ""
    password: str = ""


@dataclass
class Settings:
    """Persisted runtime state — Apple auth, MQTT (auto-discovered), flags."""
    apple: AppleConfig = field(default_factory=AppleConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
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
            apple=AppleConfig(**{k: v for k, v in raw.get("apple", {}).items()
                                 if k in AppleConfig.__dataclass_fields__}),
            mqtt=MqttConfig(**{k: v for k, v in raw.get("mqtt", {}).items()
                               if k in MqttConfig.__dataclass_fields__}),
            bundle_uploaded=raw.get("bundle_uploaded", False),
        )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(CONFIG_PATH)


_settings = Settings.load()
_addon_config = AddonConfig.load()
_lock = asyncio.Lock()


def get() -> Settings:
    return _settings


def get_addon_config() -> AddonConfig:
    return _addon_config


def reload_addon_config() -> AddonConfig:
    """Re-read options.json (e.g. after user changes config in HA UI)."""
    global _addon_config
    _addon_config = AddonConfig.load()
    return _addon_config


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
