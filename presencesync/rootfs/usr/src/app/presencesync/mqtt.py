"""MQTT publisher with HA auto-discovery — one device_tracker per tracked item.

Entity IDs use stable Apple identifiers (not device names) so renaming a device
in Find My doesn't orphan the HA entity.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading

import paho.mqtt.client as mqtt

from . import state
from .apple import LocationFix
from .icloud import DeviceFix

log = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def _slug(s: str) -> str:
    """Sanitize a string into a valid HA object_id slug."""
    s = (s or "").lower().replace(" ", "_").replace("-", "_")
    return _SLUG_RE.sub("", s).strip("_") or "unknown"


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class MqttPublisher:
    def __init__(self):
        self._client: mqtt.Client | None = None
        self._published_discovery: set[str] = set()
        self._connected = threading.Event()
        self._connect_failure_logged = False

    def configure(self) -> None:
        cfg = state.get().mqtt
        log.info("MQTT configure: %s:%s as %s", cfg.host, cfg.port, cfg.username or "(anon)")
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
        c = mqtt.Client(client_id="presencesync",
                        callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        if cfg.username:
            c.username_pw_set(cfg.username, cfg.password)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.will_set(self._availability_topic, "offline", qos=1, retain=True)
        self._client = c
        try:
            c.connect_async(cfg.host, cfg.port, keepalive=30)
            c.loop_start()
        except Exception:
            log.exception("MQTT connect failed")

    @property
    def _availability_topic(self) -> str:
        return f"{state.get().mqtt.state_prefix}/availability"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        is_failure = getattr(reason_code, "is_failure", None)
        rc_int = getattr(reason_code, "value", reason_code)
        if (is_failure is False) or rc_int == 0:
            cfg = state.get().mqtt
            log.info("MQTT connected to %s:%s", cfg.host, cfg.port)
            client.publish(self._availability_topic, "online", qos=1, retain=True)
            self._published_discovery.clear()
            self._connected.set()
            self._connect_failure_logged = False
        else:
            if not self._connect_failure_logged:
                log.error("MQTT connect failed reason=%s", reason_code)
                self._connect_failure_logged = True

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props):
        log.warning("MQTT disconnected reason=%s", reason_code)
        self._connected.clear()

    def _publish(self, topic: str, payload: str, *, retain: bool = True) -> None:
        if self._client is None:
            return
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish %s rc=%s", topic, info.rc)

    def publish_device_fix(self, d: DeviceFix) -> None:
        """Family / owned-device location → MQTT."""
        if self._client is None or not self._connected.is_set():
            return
        as_fix = LocationFix(
            identifier=d.identifier,
            name=d.name,
            model=d.model,
            latitude=d.latitude,
            longitude=d.longitude,
            horizontal_accuracy=d.horizontal_accuracy,
            timestamp_unix=d.timestamp_unix,
        )
        self.publish_fix(as_fix)
        if d.battery_level is not None:
            cfg = state.get().mqtt
            obj = f"presencesync_{_slug(d.identifier)}"
            self._publish(f"{cfg.state_prefix}/{obj}/battery", str(int(d.battery_level * 100)))
            battery_key = f"{obj}_battery"
            if battery_key not in self._published_discovery:
                self._publish_battery_discovery(obj, d, cfg)
                self._published_discovery.add(battery_key)

    def publish_fix(self, fix: LocationFix) -> None:
        if self._client is None or not self._connected.is_set():
            return
        cfg = state.get().mqtt
        home = state.get().home

        # Use stable Apple identifier for entity ID (survives device renames)
        obj = f"presencesync_{_slug(fix.identifier)}"
        if obj not in self._published_discovery:
            self._publish_discovery(obj, fix, cfg)
            self._published_discovery.add(obj)

        d = _haversine_m(fix.latitude, fix.longitude, home.latitude, home.longitude) if home.latitude else float("inf")
        state_val = "home" if d <= home.radius_m else "not_home"
        attrs = {
            "latitude": fix.latitude,
            "longitude": fix.longitude,
            "gps_accuracy": fix.horizontal_accuracy,
            "last_seen": fix.timestamp_unix,
            "friendly_name": fix.name,
            "model": fix.model or "",
            "source": "presencesync",
        }
        self._publish(f"{cfg.state_prefix}/{obj}/state", state_val)
        self._publish(f"{cfg.state_prefix}/{obj}/attributes", json.dumps(attrs))

    def _publish_discovery(self, obj: str, fix: LocationFix, cfg) -> None:
        device = {
            "identifiers": [f"presencesync_{fix.identifier}"],
            "name": fix.name,
            "manufacturer": "Apple",
            "model": fix.model or "Find My Item",
            "via_device": "presencesync",
        }
        tracker_cfg = {
            "name": None,
            "unique_id": obj,
            "object_id": obj,
            "state_topic": f"{cfg.state_prefix}/{obj}/state",
            "json_attributes_topic": f"{cfg.state_prefix}/{obj}/attributes",
            "source_type": "gps",
            "payload_home": "home",
            "payload_not_home": "not_home",
            "availability_topic": self._availability_topic,
            "device": device,
        }
        topic = f"{cfg.discovery_prefix}/device_tracker/{obj}/config"
        self._publish(topic, json.dumps(tracker_cfg))
        log.info("Discovery: %s (%s)", fix.name, obj)

    def _publish_battery_discovery(self, obj: str, d: DeviceFix, cfg) -> None:
        device = {
            "identifiers": [f"presencesync_{d.identifier}"],
            "name": d.name,
            "manufacturer": "Apple",
            "model": d.model or "Apple Device",
            "via_device": "presencesync",
        }
        sensor_cfg = {
            "name": "Battery",
            "unique_id": f"{obj}_battery",
            "state_topic": f"{cfg.state_prefix}/{obj}/battery",
            "device_class": "battery",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "availability_topic": self._availability_topic,
            "device": device,
        }
        topic = f"{cfg.discovery_prefix}/sensor/{obj}_battery/config"
        self._publish(topic, json.dumps(sensor_cfg))
        log.info("Battery discovery: %s", d.name)

    def publish_unavailable(self, device_id: str) -> None:
        """Mark a device as unavailable (stale/offline)."""
        if self._client is None or not self._connected.is_set():
            return
        cfg = state.get().mqtt
        obj = f"presencesync_{_slug(device_id)}"
        self._publish(f"{cfg.state_prefix}/{obj}/state", "unavailable")

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.publish(self._availability_topic, "offline", qos=1, retain=True)
        except Exception:
            pass
        self._client.loop_stop()
        self._client.disconnect()
