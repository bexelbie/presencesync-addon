"""Test configuration — mock external services and provide common fixtures."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure the app package is importable without installing
APP_ROOT = Path(__file__).resolve().parent.parent / "presencesync" / "rootfs" / "usr" / "src" / "app"
sys.path.insert(0, str(APP_ROOT))

# Mock only dependencies that are unavailable in the test environment.
_MOCK_MODULES = [
    "findmy", "findmy.accessory", "findmy.account", "findmy.reports",
    "findmy.reports.anisette", "findmy.plist",
    "pyicloud", "pyicloud.base",
    "urllib3", "urllib3.util", "urllib3.util.connection",
    "requests", "requests.adapters",
    "cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.ec",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.ciphers.aead",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.kdf",
    "cryptography.hazmat.primitives.kdf.hkdf",
    "cryptography.hazmat.backends",
    "cryptography.exceptions",
    "cryptography.hazmat.bindings",
    "cryptography.hazmat.bindings._rust",
    "cryptography.hazmat.bindings._rust.exceptions",
]


def _module_available(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


for mod_name in _MOCK_MODULES:
    if mod_name not in sys.modules and not _module_available(mod_name):
        sys.modules[mod_name] = MagicMock(name=mod_name)

# Fix the specific imports that the code uses from findmy when it is mocked.
if isinstance(sys.modules.get("findmy"), MagicMock):
    sys.modules["findmy"].AsyncAppleAccount = MagicMock
    sys.modules["findmy"].FindMyAccessory = MagicMock
    sys.modules["findmy"].LoginState = MagicMock
    sys.modules["findmy"].plist = MagicMock()
if isinstance(sys.modules.get("findmy.reports.anisette"), MagicMock):
    sys.modules["findmy.reports.anisette"].RemoteAnisetteProvider = MagicMock
if isinstance(sys.modules.get("findmy.plist"), MagicMock):
    sys.modules["findmy.plist"].list_accessories = MagicMock(return_value=[])


@pytest.fixture(autouse=True)
def mock_state(tmp_path, monkeypatch):
    """Provide a clean state for each test with a tmp data dir."""
    monkeypatch.setenv("PRESENCESYNC_DATA_DIR", str(tmp_path))

    from presencesync import identity, state

    monkeypatch.setattr(state, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state, "CONFIG_PATH", tmp_path / "presencesync.json")
    monkeypatch.setattr(state, "OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(state, "APPLE_STATE_PATH", tmp_path / "apple_state.pickle")
    monkeypatch.setattr(state, "DEVICE_IDENTITY_PATH", tmp_path / "device_identity.json")
    monkeypatch.setattr(state, "BUNDLE_DIR", tmp_path / "bundle")
    monkeypatch.setattr(state, "KEYS_DIR", tmp_path / "keys")
    monkeypatch.setattr(state, "_lock", asyncio.Lock())

    if "presencesync.icloud" in sys.modules:
        monkeypatch.setattr(sys.modules["presencesync.icloud"], "COOKIE_DIR", tmp_path / "pyicloud-cookies")
    if "presencesync.coordinator" in sys.modules:
        monkeypatch.setattr(sys.modules["presencesync.coordinator"], "KEYS_DIR", tmp_path / "keys")
    if "presencesync.apple" in sys.modules:
        monkeypatch.setattr(sys.modules["presencesync.apple"], "KEYS_DIR", tmp_path / "keys")
        monkeypatch.setattr(sys.modules["presencesync.apple"], "ALIGNMENT_FILE", tmp_path / "alignment.json")
    if "presencesync.extractor" in sys.modules:
        monkeypatch.setattr(sys.modules["presencesync.extractor"], "KEYS_DIR", tmp_path / "keys")

    fresh = state.Settings()
    monkeypatch.setattr(state, "_settings", fresh)
    fresh_config = state.AddonConfig()
    monkeypatch.setattr(state, "_addon_config", fresh_config)
    monkeypatch.setattr(identity, "_store", None)
    yield state


@pytest.fixture
def make_fix():
    """Factory for creating DeviceFix objects."""
    from presencesync.icloud import DeviceFix

    def _make(
        identifier="test-device-1",
        name="Test iPhone",
        latitude=37.7749,
        longitude=-122.4194,
        horizontal_accuracy=10.0,
        timestamp_unix=1700000000,
        battery_level=0.85,
        battery_status="Unplugged",
        model="iPhone 15 Pro",
        device_class="iPhone",
        ba_uuid=None,
        owner=None,
    ):
        return DeviceFix(
            identifier=identifier,
            name=name,
            model=model,
            latitude=latitude,
            longitude=longitude,
            horizontal_accuracy=horizontal_accuracy,
            timestamp_unix=timestamp_unix,
            battery_level=battery_level,
            battery_status=battery_status,
            device_class=device_class,
            ba_uuid=ba_uuid,
            owner=owner,
        )

    return _make
