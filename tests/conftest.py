"""Test configuration — mock external services and provide common fixtures."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the app package is importable without installing
APP_ROOT = Path(__file__).resolve().parent.parent / "presencesync" / "rootfs" / "usr" / "src" / "app"
sys.path.insert(0, str(APP_ROOT))

# Mock modules that aren't installed in test environment (Docker-only deps)
_MOCK_MODULES = [
    "findmy", "findmy.accessory", "findmy.account", "findmy.reports",
    "findmy.reports.anisette", "findmy.plist",
    "pyicloud", "pyicloud.base",
]
for mod_name in _MOCK_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

# Now fix the specific imports that the code uses from findmy
sys.modules["findmy"].AsyncAppleAccount = MagicMock
sys.modules["findmy"].FindMyAccessory = MagicMock
sys.modules["findmy"].LoginState = MagicMock
sys.modules["findmy"].plist = MagicMock()
sys.modules["findmy.reports.anisette"].RemoteAnisetteProvider = MagicMock
sys.modules["findmy.plist"].list_accessories = MagicMock(return_value=[])


@pytest.fixture(autouse=True)
def mock_state(tmp_path, monkeypatch):
    """Provide a clean state for each test with a tmp data dir."""
    # Patch DATA_DIR before importing state
    monkeypatch.setenv("PRESENCESYNC_DATA_DIR", str(tmp_path))

    from presencesync import state
    monkeypatch.setattr(state, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state, "CONFIG_PATH", tmp_path / "presencesync.json")

    # Initialize state with defaults — patch the module-level _settings
    fresh = state.Settings()
    monkeypatch.setattr(state, "_settings", fresh)
    # Also expose as _current for test convenience
    state._current = fresh
    yield state
    state._current = None


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
        person_id=None,
        person_name=None,
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
            person_id=person_id,
            person_name=person_name,
        )

    return _make
