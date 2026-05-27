"""Tests for state configuration and persistence."""
from __future__ import annotations

import asyncio
import json


def test_addon_config_loads_valid_options(mock_state):
    mock_state.OPTIONS_PATH.write_text(json.dumps({
        "log_level": "debug",
        "poll_interval": 120,
        "refresh_interval": 900,
        "item_poll_interval": 240,
        "stationary_radius": 75,
        "devices": [{"id": "abc123", "stationary_radius": 5}],
    }))

    config = mock_state.AddonConfig.load()

    assert config == mock_state.AddonConfig(
        log_level="debug",
        poll_interval=120,
        refresh_interval=900,
        item_poll_interval=240,
        stationary_radius=75,
        devices=[{"id": "abc123", "stationary_radius": 5}],
    )


def test_addon_config_load_missing_file_returns_defaults(mock_state):
    assert mock_state.AddonConfig.load() == mock_state.AddonConfig()


def test_addon_config_load_corrupt_json_returns_defaults(mock_state):
    mock_state.OPTIONS_PATH.write_text("{not-json")

    assert mock_state.AddonConfig.load() == mock_state.AddonConfig()


def test_reload_addon_config_updates_singleton(mock_state):
    mock_state.OPTIONS_PATH.write_text(json.dumps({
        "poll_interval": 42,
        "devices": [{"id": "device-1", "exclude": True}],
    }))

    config = mock_state.reload_addon_config()

    assert config is mock_state.get_addon_config()
    assert config.poll_interval == 42
    assert config.devices == [{"id": "device-1", "exclude": True}]


def test_settings_save_and_load_round_trip(mock_state):
    settings = mock_state.Settings(
        apple=mock_state.AppleConfig(username="apple@example.com", password="secret"),
        mqtt=mock_state.MqttConfig(
            host="mqtt.example.com",
            port=1884,
            username="mqtt-user",
            password="mqtt-pass",
        ),
        bundle_uploaded=True,
    )

    settings.save()
    loaded = mock_state.Settings.load()

    assert loaded == settings


def test_device_overrides_are_available_in_loaded_config(mock_state):
    from presencesync.identity import DeviceIdentityStore

    store = DeviceIdentityStore(path=mock_state.DEVICE_IDENTITY_PATH)
    device_id = store.register("raw-device-id", "Test Phone", "fmip")
    mock_state.OPTIONS_PATH.write_text(json.dumps({
        "stationary_radius": 50,
        "devices": [{
            "id": device_id,
            "stationary_radius": 5,
            "exclude": True,
        }],
    }))

    config = mock_state.AddonConfig.load()

    assert store.get_config(device_id, config.devices) == {
        "stationary_radius": 5,
        "exclude": True,
    }
    assert store.get_stationary_radius(device_id, config.devices, config.stationary_radius) == 5
    assert store.is_excluded(device_id, config.devices) is True


def test_update_mutator_saves_runtime_state(mock_state):
    def mutate(settings):
        settings.apple.username = "updated@example.com"
        settings.mqtt.host = "broker.internal"
        settings.bundle_uploaded = True

    updated = asyncio.run(mock_state.update(mutate))

    assert updated is mock_state.get()
    assert updated.apple.username == "updated@example.com"
    assert updated.mqtt.host == "broker.internal"
    assert updated.bundle_uploaded is True
    saved = json.loads(mock_state.CONFIG_PATH.read_text())
    assert saved["apple"]["username"] == "updated@example.com"
    assert saved["mqtt"]["host"] == "broker.internal"
    assert saved["bundle_uploaded"] is True


def test_apple_state_pickle_round_trip_and_clear(mock_state):
    payload = {"session": "abc123", "attempts": 2}

    mock_state.save_apple_state(payload)

    assert mock_state.load_apple_state() == payload
    assert mock_state.APPLE_STATE_PATH.exists()

    mock_state.clear_apple_state()

    assert mock_state.load_apple_state() is None
    assert not mock_state.APPLE_STATE_PATH.exists()
