"""Talk to HA's Supervisor + Core APIs to auto-discover sane defaults.

Available inside any HA add-on that has `hassio_api: true` + `homeassistant_api: true`
+ `services: - mqtt:want` in its config.yaml.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

SUPERVISOR_BASE = "http://supervisor"


def _get_token() -> str:
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json"}


@dataclass
class MqttInfo:
    host: str
    port: int
    username: str
    password: str


@dataclass
class HomeInfo:
    latitude: float
    longitude: float
    radius_m: float
    location_name: str


async def discover_mqtt() -> MqttInfo | None:
    """Ask Supervisor for the MQTT service."""
    if not _get_token():
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{SUPERVISOR_BASE}/services/mqtt", headers=_headers()) as r:
                if r.status != 200:
                    return None
                payload = await r.json()
        data = payload.get("data") or {}
        if not data:
            return None
        return MqttInfo(
            host=data.get("host", "core-mosquitto"),
            port=int(data.get("port", 1883)),
            username=data.get("username", ""),
            password=data.get("password", ""),
        )
    except Exception:
        log.exception("MQTT discovery failed")
        return None


async def discover_home() -> HomeInfo | None:
    """Pull latitude/longitude from HA core config + radius from zone.home."""
    if not _get_token():
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(f"{SUPERVISOR_BASE}/core/api/config", headers=_headers()) as r:
                if r.status != 200:
                    return None
                cfg = await r.json()
            lat = float(cfg.get("latitude", 0))
            lon = float(cfg.get("longitude", 0))
            loc_name = cfg.get("location_name", "Home")

            radius = 100.0
            async with s.get(f"{SUPERVISOR_BASE}/core/api/states/zone.home", headers=_headers()) as r:
                if r.status == 200:
                    zone = await r.json()
                    radius = float((zone.get("attributes") or {}).get("radius") or radius)

        return HomeInfo(latitude=lat, longitude=lon, radius_m=radius, location_name=loc_name)
    except Exception:
        log.exception("home discovery failed")
        return None
