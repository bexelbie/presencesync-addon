"""Tests for MQTT discovery publishing."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


def _make_mqtt_mock():
    """Create a MqttPublisher with a properly mocked client."""
    from presencesync.mqtt import MqttPublisher
    import paho.mqtt.client as mqtt

    pub = MqttPublisher()
    pub._client = MagicMock()
    pub._connected.set()

    # Make publish return an MQTTMessageInfo-like object
    info = MagicMock()
    info.rc = mqtt.MQTT_ERR_SUCCESS
    info.mid = 1
    pub._client.publish.return_value = info
    return pub


class TestBatteryDiscovery:
    """Battery sensor auto-discovery for HA."""

    def test_battery_discovery_published_on_first_fix(self, mock_state, make_fix):
        mock_state._current.mqtt.host = "localhost"
        mock_state._current.mqtt.discovery_prefix = "homeassistant"
        mock_state._current.mqtt.state_prefix = "presencesync"

        pub = _make_mqtt_mock()
        fix = make_fix(battery_level=0.85)
        pub.publish_device_fix(fix)

        # Should have published discovery for battery sensor
        calls = pub._client.publish.call_args_list
        topics = [c[0][0] for c in calls]

        # Find the battery discovery topic
        battery_disc = [t for t in topics if "sensor" in t and "battery" in t]
        assert len(battery_disc) == 1

        # Parse the payload
        battery_call = [c for c in calls if "sensor" in c[0][0] and "battery" in c[0][0]][0]
        payload = json.loads(battery_call[0][1])
        assert payload["device_class"] == "battery"
        assert payload["unit_of_measurement"] == "%"
        assert payload["state_class"] == "measurement"
        assert "device" in payload
        assert payload["device"]["manufacturer"] == "Apple"

    def test_battery_discovery_only_once(self, mock_state, make_fix):
        mock_state._current.mqtt.host = "localhost"
        mock_state._current.mqtt.discovery_prefix = "homeassistant"
        mock_state._current.mqtt.state_prefix = "presencesync"

        pub = _make_mqtt_mock()
        fix = make_fix(battery_level=0.85)
        pub.publish_device_fix(fix)
        pub.publish_device_fix(fix)

        # Battery discovery should appear only once across both publishes
        calls = pub._client.publish.call_args_list
        battery_disc = [c for c in calls if "sensor" in c[0][0] and "battery" in c[0][0]]
        assert len(battery_disc) == 1

    def test_no_battery_discovery_when_level_is_none(self, mock_state, make_fix):
        mock_state._current.mqtt.host = "localhost"
        mock_state._current.mqtt.discovery_prefix = "homeassistant"
        mock_state._current.mqtt.state_prefix = "presencesync"

        pub = _make_mqtt_mock()
        fix = make_fix(battery_level=None)
        pub.publish_device_fix(fix)

        calls = pub._client.publish.call_args_list
        battery_disc = [c for c in calls if "sensor" in c[0][0] and "battery" in c[0][0]]
        assert len(battery_disc) == 0


class TestPublishUnavailable:
    """Stale device unavailability marking."""

    def test_publishes_unavailable_state(self, mock_state, make_fix):
        mock_state._current.mqtt.state_prefix = "presencesync"

        pub = _make_mqtt_mock()
        pub.publish_unavailable("dev-123", "Test iPhone")

        calls = pub._client.publish.call_args_list
        # Should publish "unavailable" to the device state topic
        state_calls = [c for c in calls if "presencesync_test_iphone/state" in c[0][0]]
        assert len(state_calls) == 1
        assert state_calls[0][0][1] == "unavailable"
