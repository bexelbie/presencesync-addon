# ABOUTME: Publishes PresenceSync MQTT discovery, state, and command topics for Home Assistant.
# ABOUTME: Manages broker connectivity, availability, and command routing for tracked devices.
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from . import state

log = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
STATE_PREFIX = "presencesync"
GLOBAL_AVAILABILITY_TOPIC = f"{STATE_PREFIX}/availability"
HUB_IDENTIFIER = "presencesync_hub"
COMMAND_TOPICS = {
    "poll": f"{STATE_PREFIX}/command/poll",
    "refresh": f"{STATE_PREFIX}/command/refresh",
    "fetch_items": f"{STATE_PREFIX}/command/fetch_items",
}
PLAY_SOUND_SUBSCRIPTION = f"{STATE_PREFIX}/+/play_sound/set"


class MqttPublisher:
    def __init__(self) -> None:
        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._callbacks: dict[str, Callable[..., Any]] = {}

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def connect(self) -> None:
        cfg = state.get().mqtt
        if self._client is not None:
            self.stop()

        client = mqtt.Client(
            client_id="presencesync",
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.will_set(GLOBAL_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)

        self._client = client
        try:
            client.connect(cfg.host, int(cfg.port), keepalive=30)
            client.loop_start()
        except Exception:
            self._client = None
            self._connected.clear()
            log.exception("Failed to connect to MQTT broker")

    def stop(self) -> None:
        client = self._client
        if client is None:
            return

        try:
            client.publish(GLOBAL_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        except Exception:
            log.exception("Failed to publish MQTT offline availability")

        try:
            client.disconnect()
        finally:
            client.loop_stop()
            self._connected.clear()
            self._client = None

    def publish_hub_discovery(self) -> None:
        device = {
            "identifiers": [HUB_IDENTIFIER],
            "name": "PresenceSync",
            "manufacturer": "PresenceSync",
            "model": "Hub",
        }
        buttons = (
            ("poll", "Poll iDevices", COMMAND_TOPICS["poll"]),
            ("refresh", "Refresh iDevices", COMMAND_TOPICS["refresh"]),
            ("fetch_items", "Fetch Items", COMMAND_TOPICS["fetch_items"]),
        )

        for suffix, name, command_topic in buttons:
            unique_id = f"presencesync_{suffix}"
            self._publish_json(
                f"{DISCOVERY_PREFIX}/button/{unique_id}/config",
                {
                    "name": name,
                    "unique_id": unique_id,
                    "command_topic": command_topic,
                    "availability_topic": GLOBAL_AVAILABILITY_TOPIC,
                    "device": device,
                },
            )

    def publish_device_discovery(
        self,
        device_id: str,
        name: str,
        model: str | None,
        has_battery: bool,
        has_play_sound: bool,
    ) -> None:
        unique_id = self._device_unique_id(device_id)
        device = self._device_payload(device_id, name, model)

        self._publish_json(
            f"{DISCOVERY_PREFIX}/device_tracker/{unique_id}/config",
            {
                "name": None,
                "unique_id": unique_id,
                "object_id": unique_id,
                "json_attributes_topic": f"{STATE_PREFIX}/{device_id}/attributes",
                "source_type": "gps",
                "availability": self._device_availability(device_id),
                "device": device,
            },
        )

        if has_battery:
            self._publish_json(
                f"{DISCOVERY_PREFIX}/sensor/{unique_id}_battery/config",
                {
                    "name": "Battery",
                    "unique_id": f"{unique_id}_battery",
                    "state_topic": f"{STATE_PREFIX}/{device_id}/battery",
                    "device_class": "battery",
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                    "availability": self._device_availability(device_id),
                    "device": device,
                },
            )

        if has_play_sound:
            self._publish_json(
                f"{DISCOVERY_PREFIX}/button/{unique_id}_play_sound/config",
                {
                    "name": "Play Sound",
                    "unique_id": f"{unique_id}_play_sound",
                    "command_topic": f"{STATE_PREFIX}/{device_id}/play_sound/set",
                    "availability": self._device_availability(device_id),
                    "device_class": "identify",
                    "device": device,
                },
            )

    def publish_location(self, device_id: str, attrs: dict) -> None:
        payload = {
            "latitude": float(attrs["latitude"]),
            "longitude": float(attrs["longitude"]),
            "gps_accuracy": attrs["gps_accuracy"],
            "last_seen": attrs["last_seen"],
            "friendly_name": attrs["friendly_name"],
            "model": attrs.get("model"),
            "source": attrs["source"],
        }
        if attrs.get("owner") is not None:
            payload["owner"] = attrs["owner"]
        if attrs.get("shared_date") is not None:
            payload["shared_date"] = attrs["shared_date"]

        self._publish_json(f"{STATE_PREFIX}/{device_id}/attributes", payload)

    def publish_battery(self, device_id: str, percentage: int) -> None:
        self._publish(f"{STATE_PREFIX}/{device_id}/battery", str(int(percentage)))

    def publish_device_availability(self, device_id: str, available: bool) -> None:
        self._publish(
            f"{STATE_PREFIX}/{device_id}/availability",
            "online" if available else "offline",
        )

    def subscribe_commands(self, callbacks: dict) -> None:
        self._callbacks = dict(callbacks)
        self._subscribe_to_commands()

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        failed = getattr(reason_code, "is_failure", None)
        if failed is None:
            failed = int(reason_code) != 0

        if failed:
            log.error("MQTT connect failed: %s", reason_code)
            self._connected.clear()
            return

        self._connected.set()
        client.publish(GLOBAL_AVAILABILITY_TOPIC, "online", qos=1, retain=True)
        self._subscribe_to_commands()

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        log.info("MQTT disconnected: %s", reason_code)
        self._connected.clear()

    def _on_message(self, _client, _userdata, message) -> None:
        topic = message.topic
        if topic == COMMAND_TOPICS["poll"]:
            self._invoke_callback("poll")
            return
        if topic == COMMAND_TOPICS["refresh"]:
            self._invoke_callback("refresh")
            return
        if topic == COMMAND_TOPICS["fetch_items"]:
            self._invoke_callback("fetch_items")
            return

        parts = topic.split("/")
        if len(parts) == 4 and parts[0] == STATE_PREFIX and parts[2] == "play_sound" and parts[3] == "set":
            self._invoke_callback("play_sound", parts[1])

    def _invoke_callback(self, name: str, *args: Any) -> None:
        callback = self._callbacks.get(name)
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            log.exception("MQTT callback failed for %s", name)

    def _subscribe_to_commands(self) -> None:
        client = self._client
        if client is None or not self.connected:
            return

        for topic in (*COMMAND_TOPICS.values(), PLAY_SOUND_SUBSCRIPTION):
            result, _mid = client.subscribe(topic, qos=1)
            if result != mqtt.MQTT_ERR_SUCCESS:
                log.warning("MQTT subscribe failed for %s: %s", topic, result)

    def _publish_json(self, topic: str, payload: dict[str, Any]) -> None:
        self._publish(topic, json.dumps(payload))

    def _publish(self, topic: str, payload: str) -> None:
        client = self._client
        if client is None:
            return
        info = client.publish(topic, payload, qos=1, retain=True)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("MQTT publish failed for %s: %s", topic, info.rc)

    def _device_unique_id(self, device_id: str) -> str:
        return f"presencesync_{device_id}"

    def _device_payload(self, device_id: str, name: str, model: str | None) -> dict[str, Any]:
        return {
            "identifiers": [self._device_unique_id(device_id)],
            "name": name,
            "manufacturer": "Apple",
            "model": model,
            "via_device": HUB_IDENTIFIER,
        }

    def _device_availability(self, device_id: str) -> list[dict[str, str]]:
        return [
            {
                "topic": GLOBAL_AVAILABILITY_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": f"{STATE_PREFIX}/{device_id}/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ]
