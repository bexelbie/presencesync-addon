from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt


def _make_message(topic: str):
    message = MagicMock()
    message.topic = topic
    message.payload = b""
    return message


def _make_publish_info():
    info = MagicMock()
    info.rc = mqtt.MQTT_ERR_SUCCESS
    return info


def _make_publisher():
    from presencesync.mqtt import MqttPublisher

    publisher = MqttPublisher()
    publisher._client = MagicMock()
    publisher._client.publish.return_value = _make_publish_info()
    publisher._client.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
    publisher._connected.set()
    return publisher


def test_connect_uses_state_credentials_and_version2(mock_state):
    mock_state.get().mqtt.host = "mqtt.example.com"
    mock_state.get().mqtt.port = 1884
    mock_state.get().mqtt.username = "alice"
    mock_state.get().mqtt.password = "secret"

    fake_client = MagicMock()
    with patch("presencesync.mqtt.mqtt.Client", return_value=fake_client) as client_cls:
        from presencesync.mqtt import MqttPublisher

        publisher = MqttPublisher()
        publisher.connect()

    client_cls.assert_called_once_with(
        client_id="presencesync",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    fake_client.username_pw_set.assert_called_once_with("alice", "secret")
    fake_client.will_set.assert_called_once_with(
        "presencesync/availability",
        "offline",
        qos=1,
        retain=True,
    )
    fake_client.connect.assert_called_once_with("mqtt.example.com", 1884, keepalive=30)
    fake_client.loop_start.assert_called_once_with()


def test_connect_handles_broker_errors(mock_state):
    fake_client = MagicMock()
    fake_client.connect.side_effect = OSError("broker down")
    with patch("presencesync.mqtt.mqtt.Client", return_value=fake_client):
        from presencesync.mqtt import MqttPublisher

        publisher = MqttPublisher()
        publisher.connect()

    assert publisher._client is None
    fake_client.loop_start.assert_not_called()


def test_publish_hub_discovery(mock_state):
    publisher = _make_publisher()

    publisher.publish_hub_discovery()

    calls = publisher._client.publish.call_args_list
    topics = {call.args[0] for call in calls}
    assert topics == {
        "homeassistant/button/presencesync_poll/config",
        "homeassistant/button/presencesync_refresh/config",
        "homeassistant/button/presencesync_fetch_items/config",
    }

    payloads = {call.args[0]: json.loads(call.args[1]) for call in calls}
    assert payloads["homeassistant/button/presencesync_poll/config"]["command_topic"] == "presencesync/command/poll"
    assert payloads["homeassistant/button/presencesync_refresh/config"]["command_topic"] == "presencesync/command/refresh"
    assert payloads["homeassistant/button/presencesync_fetch_items/config"]["command_topic"] == "presencesync/command/fetch_items"
    for payload in payloads.values():
        assert payload["availability_topic"] == "presencesync/availability"
        assert payload["device"] == {
            "identifiers": ["presencesync_hub"],
            "name": "PresenceSync",
            "manufacturer": "PresenceSync",
            "model": "Hub",
        }


def test_publish_device_discovery_matches_spec(mock_state):
    publisher = _make_publisher()

    publisher.publish_device_discovery(
        device_id="a1b2c3d4e5f6",
        name="B iPhone",
        model="iPhone 13 Pro",
        has_battery=True,
        has_play_sound=True,
    )

    payloads = {
        call.args[0]: json.loads(call.args[1])
        for call in publisher._client.publish.call_args_list
    }

    tracker = payloads["homeassistant/device_tracker/presencesync_a1b2c3d4e5f6/config"]
    assert tracker["json_attributes_topic"] == "presencesync/a1b2c3d4e5f6/attributes"
    assert tracker["source_type"] == "gps"
    assert "state_topic" not in tracker
    assert tracker["availability"] == [
        {
            "topic": "presencesync/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        },
        {
            "topic": "presencesync/a1b2c3d4e5f6/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        },
    ]
    assert tracker["device"] == {
        "identifiers": ["presencesync_a1b2c3d4e5f6"],
        "name": "B iPhone",
        "manufacturer": "Apple",
        "model": "iPhone 13 Pro",
        "via_device": "presencesync_hub",
    }

    battery = payloads["homeassistant/sensor/presencesync_a1b2c3d4e5f6_battery/config"]
    assert battery["state_topic"] == "presencesync/a1b2c3d4e5f6/battery"
    assert battery["device_class"] == "battery"
    assert battery["unit_of_measurement"] == "%"
    assert battery["state_class"] == "measurement"

    play_sound = payloads["homeassistant/button/presencesync_a1b2c3d4e5f6_play_sound/config"]
    assert play_sound["command_topic"] == "presencesync/a1b2c3d4e5f6/play_sound/set"
    assert play_sound["device_class"] == "identify"


def test_publish_location_uses_float_coordinates_and_excludes_source_type(mock_state):
    publisher = _make_publisher()

    publisher.publish_location(
        "a1b2c3d4e5f6",
        {
            "latitude": "37.7749",
            "longitude": "-122.4194",
            "gps_accuracy": 5,
            "last_seen": 1700000000,
            "friendly_name": "B iPhone",
            "model": "iPhone 13 Pro",
            "source": "icloud",
            "source_type": "gps",
            "owner": "bex",
            "shared_date": 1700000001,
        },
    )

    call = publisher._client.publish.call_args_list[-1]
    assert call.args[0] == "presencesync/a1b2c3d4e5f6/attributes"
    payload = json.loads(call.args[1])
    assert payload["latitude"] == 37.7749
    assert isinstance(payload["latitude"], float)
    assert payload["longitude"] == -122.4194
    assert isinstance(payload["longitude"], float)
    assert payload["owner"] == "bex"
    assert payload["shared_date"] == 1700000001
    assert "source_type" not in payload


def test_subscribe_commands_routes_callbacks(mock_state):
    publisher = _make_publisher()
    callbacks = {
        "poll": MagicMock(),
        "refresh": MagicMock(),
        "fetch_items": MagicMock(),
        "play_sound": MagicMock(),
    }

    publisher.subscribe_commands(callbacks)

    subscribed_topics = {call.args[0] for call in publisher._client.subscribe.call_args_list}
    assert subscribed_topics == {
        "presencesync/command/poll",
        "presencesync/command/refresh",
        "presencesync/command/fetch_items",
        "presencesync/+/play_sound/set",
    }

    publisher._on_message(None, None, _make_message("presencesync/command/poll"))
    publisher._on_message(None, None, _make_message("presencesync/command/refresh"))
    publisher._on_message(None, None, _make_message("presencesync/command/fetch_items"))
    publisher._on_message(None, None, _make_message("presencesync/a1b2c3d4e5f6/play_sound/set"))

    callbacks["poll"].assert_called_once_with()
    callbacks["refresh"].assert_called_once_with()
    callbacks["fetch_items"].assert_called_once_with()
    callbacks["play_sound"].assert_called_once_with("a1b2c3d4e5f6")
