"""Tests for stable device identity persistence."""
from __future__ import annotations

import json
import logging


def test_hash_device_id_is_stable_and_short(mock_state):
    from presencesync.identity import hash_device_id

    first = hash_device_id("device-123")
    second = hash_device_id("device-123")
    other = hash_device_id("device-456")

    assert first == second
    assert len(first) == 12
    assert first.islower()
    assert first != other


def test_register_persists_and_updates_device_name(mock_state):
    from presencesync.identity import DeviceIdentityStore

    store = DeviceIdentityStore()
    device_hash = store.register("raw-device-id", "Office Keys", "item")
    updated_hash = store.register("raw-device-id", "Keys", "shared_item")
    reloaded = DeviceIdentityStore()

    assert updated_hash == device_hash

    saved = json.loads((mock_state.DATA_DIR / "device_identity.json").read_text())
    assert saved == {
        "devices": {
            device_hash: {
                "raw_id": "raw-device-id",
                "name": "Keys",
                "source": "shared_item",
            }
        }
    }
    assert reloaded._data == saved


def test_get_config_supports_list_and_helpers(mock_state):
    from presencesync.identity import DeviceIdentityStore

    store = DeviceIdentityStore()
    device_hash = store.get_hash("raw-device-id")
    addon_devices = [
        {
            "id": device_hash,
            "stationary_radius": 0,
            "exclude": True,
        },
        {"id": "other-device", "stationary_radius": 50},
    ]

    assert store.get_config(device_hash, addon_devices) == {
        "stationary_radius": 0,
        "exclude": True,
    }
    assert store.get_config(device_hash, {"devices": addon_devices}) == {
        "stationary_radius": 0,
        "exclude": True,
    }
    assert store.is_excluded(device_hash, addon_devices) is True
    assert store.get_stationary_radius(device_hash, addon_devices, 25) == 0
    assert store.get_stationary_radius("missing", addon_devices, 25) == 25
    assert store.is_excluded("missing", addon_devices) is False


def test_log_device_table_logs_known_devices(mock_state, caplog):
    from presencesync.identity import DeviceIdentityStore

    store = DeviceIdentityStore()
    device_hash = store.register("raw-device-id-abcdefghijklmnopqrstuvwxyz", "Backpack", "fmip")

    with caplog.at_level(logging.INFO):
        store.log_device_table()

    assert "Known devices:" in caplog.text
    assert device_hash in caplog.text
    assert "Backpack" in caplog.text
    assert "fmip" in caplog.text
