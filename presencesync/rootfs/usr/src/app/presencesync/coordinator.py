# ABOUTME: Coordinates fixed-interval Find My polling and item fetches and publishes results to MQTT.
# ABOUTME: Applies stationary denoising, deduplicates beacon reports, and tracks device availability.
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path

from findmy import LoginState

from . import anisette_manager
from . import identity
from . import state
from . import supervisor
from .apple import AppleClient, LocationFix
from .icloud import DeviceFix, ICloudClient
from .mqtt import MqttPublisher

log = logging.getLogger(__name__)

KEYS_DIR = state.DATA_DIR / "keys"
KNOWN_DEVICES_PATH = state.DATA_DIR / "known_devices.json"


def _load_cloudkit_to_stable_map(metadata_dir: Path | None = None) -> dict[str, str]:
    """Load metadata.json and map cloudkit_record_id to stable beacon identifier."""
    meta_root = metadata_dir or Path(KEYS_DIR)
    meta_path = meta_root / "metadata.json"
    if not meta_path.exists():
        return {}

    try:
        payload = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError):
        log.exception("Failed to load %s", meta_path)
        return {}

    mapping: dict[str, str] = {}
    for stable_id, record in (payload.get("beacon_records") or {}).items():
        if not isinstance(record, dict):
            continue
        cloudkit_record_id = record.get("cloudkit_record_id") or ""
        if cloudkit_record_id:
            mapping[cloudkit_record_id] = stable_id
    return mapping


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two lat/lon pairs in meters."""
    radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius_m * math.asin(math.sqrt(a))


class Coordinator:
    def __init__(self):
        self.apple = AppleClient()
        self.icloud = ICloudClient()
        self.mqtt = MqttPublisher()

        self.last_run_unix: int = 0
        self.last_fixes: list[LocationFix] = []
        self.last_device_fixes: list[DeviceFix] = []

        self._anchors: dict[str, tuple[float, float]] = {}
        self._availability: dict[str, bool] = {}
        self._prev_idevice_ids: set[str] = set()
        self._prev_item_ids: set[str] = set()

        self._fmi_ts_by_beacon_id: dict[str, int] = {}
        self._fmi_id_by_beacon_id: dict[str, str] = {}
        self._raw_icloud_ids_by_device_id: dict[str, str] = {}
        self._published_timestamps: dict[str, int] = {}
        self._metadata_dir: Path = Path(KEYS_DIR)
        self._ck_to_stable = _load_cloudkit_to_stable_map(self._metadata_dir)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._item_task: asyncio.Task | None = None
        self._command_tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()
        self._device_lock = asyncio.Lock()
        self._item_lock = asyncio.Lock()

        self._icloud_consecutive_failures = 0
        self._icloud_ctx_reset_interval = 2 * 3600
        self._icloud_last_ctx_reset = 0.0

    async def start(self):
        if any(task is not None and not task.done() for task in (self._poll_task, self._refresh_task, self._item_task)):
            return

        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()

        await self._auto_discover_mqtt()
        self._connect_mqtt()
        await self._wait_for_mqtt_connection()
        self._subscribe_commands()
        self._publish_hub_discovery()

        mgr = anisette_manager.get()
        if not await mgr.ensure_running():
            log.warning("Anisette server not available")

        try:
            await self.apple.ensure_account()
        except Exception:
            log.exception("apple.ensure_account failed")

        self._load_item_keys()
        self._reload_cloudkit_mapping()
        self._load_known_devices()
        await self._resume_icloud_session()

        has_keys = bool(self.apple.accessories or self.apple.shared_accessories)
        if has_keys:
            await self._initial_data_fetch()

        self._poll_task = self._create_loop_task(self._poll_loop(), "poll")
        self._refresh_task = self._create_loop_task(self._refresh_loop(), "refresh")
        self._item_task = self._create_loop_task(self._item_loop(), "items")

    async def stop(self):
        self._stop_event.set()

        tasks = [task for task in (self._poll_task, self._refresh_task, self._item_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._poll_task = None
        self._refresh_task = None
        self._item_task = None

        for task in list(self._command_tasks):
            task.cancel()
        if self._command_tasks:
            await asyncio.gather(*list(self._command_tasks), return_exceptions=True)
        self._command_tasks.clear()

        self.mqtt.stop()

    async def _initial_data_fetch(self):
        """Run initial refresh + item fetch + warmup polls after keys are available."""
        await self._do_refresh()
        await self._do_fetch_items()
        identity.get().log_device_table()
        # Apple needs time to wake devices after refresh.
        # Poll at +30s and +60s to pick up trickle-in data.
        self._create_loop_task(self._warmup_polls(), "warmup")

    async def _warmup_polls(self):
        """Poll at +30s and +60s to catch trickle-in data after a refresh."""
        for delay in (30, 30):
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                log.info("Warmup poll")
                await self._do_poll()

    async def _poll_loop(self):
        while not self._stop_event.is_set():
            interval = int(state.get_addon_config().poll_interval)
            if interval <= 0:
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                await self._do_poll()

    async def _refresh_loop(self):
        while not self._stop_event.is_set():
            interval = int(state.get_addon_config().refresh_interval)
            if interval <= 0:
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                await self._do_refresh()

    async def _item_loop(self):
        while not self._stop_event.is_set():
            interval = int(state.get_addon_config().item_poll_interval)
            if interval <= 0:
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                await self._do_fetch_items()

    async def _do_poll(self):
        async with self._device_lock:
            self._reload_cloudkit_mapping()
            if not await self._ensure_icloud_session():
                self.last_device_fixes = []
                return

            try:
                device_fixes = await asyncio.get_event_loop().run_in_executor(
                    None, self.icloud.poll_devices
                )
                self._icloud_consecutive_failures = 0
            except Exception:
                await self._handle_icloud_failure("poll")
                return

            self.last_device_fixes = device_fixes
            self.last_run_unix = int(time.time())
            for device_fix in device_fixes:
                await self._process_idevice(device_fix)
            self._update_idevice_availability(device_fixes)

    async def _do_refresh(self):
        async with self._device_lock:
            self._reload_cloudkit_mapping()
            if not await self._ensure_icloud_session():
                self.last_device_fixes = []
                return

            try:
                self._maybe_reset_icloud_server_context()
                device_fixes = await asyncio.get_event_loop().run_in_executor(
                    None, self.icloud.refresh_devices
                )
                self._icloud_consecutive_failures = 0
            except Exception:
                await self._handle_icloud_failure("refresh")
                return

            self.last_device_fixes = device_fixes
            self.last_run_unix = int(time.time())
            for device_fix in device_fixes:
                await self._process_idevice(device_fix)

    async def _do_fetch_items(self):
        async with self._item_lock:
            self._reload_cloudkit_mapping()
            if self.apple.last_login_state != LoginState.LOGGED_IN:
                self.last_fixes = []
                return
            if not self.apple.accessories and not self.apple.shared_accessories:
                self.last_fixes = []
                return

            fixes: list[LocationFix] = []
            try:
                if self.apple.accessories:
                    fixes.extend(await self.apple.fetch_locations())
                if self.apple.shared_accessories:
                    fixes.extend(await self.apple.fetch_shared_location_fixes())
            except Exception:
                log.exception("Item fetch failed")
                return

            self.last_fixes = fixes
            self.last_run_unix = int(time.time())
            for fix in fixes:
                await self._process_item(fix)
            self._update_item_availability(fixes)

    async def _process_idevice(self, d: DeviceFix):
        store = identity.get()
        config = state.get_addon_config()
        device_id = store.register(d.identifier, d.name, "fmip")
        if store.is_excluded(device_id, config.devices):
            return

        self._raw_icloud_ids_by_device_id[device_id] = d.identifier

        if d.ba_uuid:
            beacon_id = self._ck_to_stable.get(d.ba_uuid)
            if beacon_id:
                self._fmi_ts_by_beacon_id[beacon_id] = max(
                    self._fmi_ts_by_beacon_id.get(beacon_id, 0),
                    d.timestamp_unix,
                )
                self._fmi_id_by_beacon_id[beacon_id] = d.identifier

        if d.timestamp_unix <= self._published_timestamps.get(device_id, 0):
            return

        lat, lon = self._apply_stationary(device_id, d.latitude, d.longitude)
        attrs = {
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": d.horizontal_accuracy,
            "last_seen": d.timestamp_unix,
            "friendly_name": d.name,
            "model": d.model or "",
            "source": "presencesync",
        }
        if d.owner:
            attrs["owner"] = d.owner

        self._publish_device_discovery(
            device_id=device_id,
            name=d.name,
            model=d.model,
            has_battery=d.battery_level is not None,
            has_play_sound=True,
        )
        self._publish_location(device_id, attrs, source_fix=d)
        if d.battery_level is not None and d.battery_level > 0:
            self._publish_battery(device_id, int(d.battery_level * 100))

        self._published_timestamps[device_id] = max(
            self._published_timestamps.get(device_id, 0),
            d.timestamp_unix,
        )

    async def _process_item(self, fix: LocationFix):
        store = identity.get()
        config = state.get_addon_config()
        ident = fix.identifier or ""
        is_idevice_beacon = ident.startswith("l:/") or ident.startswith("me:/")

        if is_idevice_beacon:
            fmi_ts = self._fmi_ts_by_beacon_id.get(ident, 0)
            if fmi_ts >= fix.timestamp_unix:
                return
            fmi_id = self._fmi_id_by_beacon_id.get(ident)
            if not fmi_id:
                # No correlation to FMiP device — skip to avoid duplicate HA entries.
                # FMiP is primary for iDevices; beacon data only publishes when correlated.
                return
            device_id = store.get_hash(fmi_id)
        else:
            device_id = store.register(ident, fix.name, "item")

        if store.is_excluded(device_id, config.devices):
            return
        if fix.timestamp_unix <= self._published_timestamps.get(device_id, 0):
            return

        lat, lon = self._apply_stationary(device_id, fix.latitude, fix.longitude)
        attrs = {
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": fix.horizontal_accuracy,
            "last_seen": fix.timestamp_unix,
            "friendly_name": fix.name,
            "model": fix.model or "Find My Item",
            "source": "presencesync",
        }
        if fix.shared_by:
            attrs["owner"] = fix.shared_by
        if fix.shared_date:
            attrs["shared_date"] = fix.shared_date

        # Skip discovery for correlated iDevice beacons — FMiP already published it
        if not is_idevice_beacon:
            self._publish_device_discovery(
                device_id=device_id,
                name=fix.name,
                model=fix.model,
                has_battery=False,
                has_play_sound=False,
            )
        self._publish_location(device_id, attrs, source_fix=fix)

        self._published_timestamps[device_id] = max(
            self._published_timestamps.get(device_id, 0),
            fix.timestamp_unix,
        )

    def _apply_stationary(self, device_id: str, lat: float, lon: float) -> tuple[float, float]:
        """Return anchor coordinates while the device stays within its stationary radius."""
        addon_config = state.get_addon_config()
        radius = identity.get().get_stationary_radius(
            device_id,
            addon_config.devices,
            addon_config.stationary_radius,
        )
        if radius == 0:
            return (lat, lon)

        anchor = self._anchors.get(device_id)
        if anchor is None:
            self._anchors[device_id] = (lat, lon)
            return (lat, lon)

        distance = _haversine_m(lat, lon, anchor[0], anchor[1])
        if distance <= radius:
            return anchor

        self._anchors[device_id] = (lat, lon)
        return (lat, lon)

    def _load_known_devices(self):
        """Load cached device sets from last run to avoid false offlines on startup."""
        path = KNOWN_DEVICES_PATH
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            self._prev_idevice_ids = set(data.get("idevices", []))
            self._prev_item_ids = set(data.get("items", []))
            log.info(
                "Loaded known devices cache: %d idevices, %d items",
                len(self._prev_idevice_ids),
                len(self._prev_item_ids),
            )
        except (OSError, json.JSONDecodeError):
            log.warning("Failed to load known_devices.json, starting fresh")

    def _save_known_devices(self):
        """Persist current device sets so the next startup can avoid false offlines."""
        data = {
            "idevices": sorted(self._prev_idevice_ids),
            "items": sorted(self._prev_item_ids),
        }
        try:
            KNOWN_DEVICES_PATH.write_text(json.dumps(data))
        except OSError:
            log.exception("Failed to save known_devices.json")

    def _update_idevice_availability(self, device_fixes: list[DeviceFix]):
        """Mark iDevices offline if Apple stopped reporting them."""
        store = identity.get()
        config = state.get_addon_config()
        current_ids: set[str] = set()
        for d in device_fixes:
            device_id = store.register(d.identifier, d.name, "fmip")
            if not store.is_excluded(device_id, config.devices):
                current_ids.add(device_id)

        disappeared = self._prev_idevice_ids - current_ids
        for device_id in disappeared:
            self._set_availability(device_id, False)

        for device_id in current_ids:
            self._set_availability(device_id, True)

        self._prev_idevice_ids = current_ids
        self._save_known_devices()

    def _update_item_availability(self, fixes: list[LocationFix]):
        """Mark items offline if Apple stopped reporting them."""
        store = identity.get()
        config = state.get_addon_config()
        current_ids: set[str] = set()
        for fix in fixes:
            ident = fix.identifier or ""
            is_idevice_beacon = ident.startswith("l:/") or ident.startswith("me:/")
            if is_idevice_beacon:
                continue
            device_id = store.register(ident, fix.name, "item")
            if not store.is_excluded(device_id, config.devices):
                current_ids.add(device_id)

        disappeared = self._prev_item_ids - current_ids
        for device_id in disappeared:
            self._set_availability(device_id, False)

        for device_id in current_ids:
            self._set_availability(device_id, True)

        self._prev_item_ids = current_ids
        self._save_known_devices()

    async def _do_play_sound(self, device_id: str):
        raw_device_id = self._raw_icloud_ids_by_device_id.get(device_id, device_id)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.icloud.play_sound(raw_device_id, "Find My iPhone Alert"),
            )
        except Exception:
            log.exception("play_sound failed for %s", raw_device_id)

    async def poll_now(self):
        await self._do_poll()
        await self._do_fetch_items()

    async def reload_mqtt(self):
        self._connect_mqtt()
        await self._wait_for_mqtt_connection()
        self._subscribe_commands()
        self._publish_hub_discovery()

    async def _auto_discover_mqtt(self):
        mqtt_info = await supervisor.discover_mqtt()
        if mqtt_info is None:
            return

        await state.update(
            lambda s: (
                setattr(s.mqtt, "host", mqtt_info.host),
                setattr(s.mqtt, "port", mqtt_info.port),
                setattr(s.mqtt, "username", mqtt_info.username),
                setattr(s.mqtt, "password", mqtt_info.password),
            )
        )

    def _load_item_keys(self):
        keys_loaded = False
        try:
            self.apple.load_keys_dir()
            keys_loaded = bool(self.apple.accessories or self.apple.shared_accessories)
            if keys_loaded:
                self._metadata_dir = Path(KEYS_DIR)
        except Exception:
            log.debug("No keys available in %s", KEYS_DIR, exc_info=True)

        if not keys_loaded and state.get().bundle_uploaded:
            try:
                self.apple.load_bundle(state.BUNDLE_DIR)
                self._metadata_dir = state.BUNDLE_DIR
            except Exception:
                log.exception("Failed to auto-load bundle from %s", state.BUNDLE_DIR)

    async def _resume_icloud_session(self):
        settings = state.get()
        if not settings.apple.username or not settings.apple.password:
            return
        try:
            login_state = await asyncio.get_event_loop().run_in_executor(
                None,
                self.icloud.login,
                settings.apple.username,
                settings.apple.password,
            )
            log.info("icloud auto-resume: %s", login_state)
        except Exception:
            log.exception("icloud auto-resume failed")

    def _reload_cloudkit_mapping(self):
        self._ck_to_stable = _load_cloudkit_to_stable_map(self._metadata_dir)

    async def _ensure_icloud_session(self) -> bool:
        if self.icloud.login_state == "logged_in":
            return True

        settings = state.get()
        if not settings.apple.username or not settings.apple.password:
            return False

        try:
            login_state = await asyncio.get_event_loop().run_in_executor(
                None,
                self.icloud.login,
                settings.apple.username,
                settings.apple.password,
            )
            log.info("icloud session restore: %s", login_state)
        except Exception:
            log.exception("icloud session restore failed")
            return False
        return self.icloud.login_state == "logged_in"

    async def _handle_icloud_failure(self, operation: str):
        self._icloud_consecutive_failures += 1
        log.exception("iCloud %s failed", operation)
        if self._icloud_consecutive_failures < 3:
            return

        settings = state.get()
        if not settings.apple.username or not settings.apple.password:
            log.warning("Skipping iCloud recovery; no stored credentials")
            return

        try:
            login_state = await asyncio.get_event_loop().run_in_executor(
                None,
                self.icloud.login,
                settings.apple.username,
                settings.apple.password,
            )
            log.info("iCloud session recovery: %s", login_state)
            if login_state == "logged_in":
                self._icloud_consecutive_failures = 0
        except Exception:
            log.exception("iCloud session recovery failed")

    def _maybe_reset_icloud_server_context(self):
        now = time.time()
        if now - self._icloud_last_ctx_reset < self._icloud_ctx_reset_interval:
            return
        self.icloud.reset_server_context()
        self._icloud_last_ctx_reset = now

    def _connect_mqtt(self):
        if hasattr(self.mqtt, "connect"):
            self.mqtt.connect()
            return
        if hasattr(self.mqtt, "configure"):
            self.mqtt.configure()

    async def _wait_for_mqtt_connection(self, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.mqtt.connected:
                return
            await asyncio.sleep(0.1)
        log.warning("MQTT not connected after %.1fs", timeout)

    def _subscribe_commands(self):
        callbacks = {
            "poll": lambda: self._schedule(self._do_poll()),
            "refresh": lambda: self._schedule(self._do_refresh()),
            "fetch_items": lambda: self._schedule(self._do_fetch_items()),
            "play_sound": lambda device_id: self._schedule(self._do_play_sound(device_id)),
        }
        if hasattr(self.mqtt, "subscribe_commands"):
            self.mqtt.subscribe_commands(callbacks)
            return
        if hasattr(self.mqtt, "set_play_sound_callback"):
            self.mqtt.set_play_sound_callback(lambda device_id: callbacks["play_sound"](device_id))

    def _publish_hub_discovery(self):
        if hasattr(self.mqtt, "publish_hub_discovery"):
            self.mqtt.publish_hub_discovery()

    def _publish_device_discovery(
        self,
        *,
        device_id: str,
        name: str,
        model: str | None,
        has_battery: bool,
        has_play_sound: bool,
    ):
        if hasattr(self.mqtt, "publish_device_discovery"):
            self.mqtt.publish_device_discovery(device_id, name, model, has_battery, has_play_sound)

    def _publish_location(self, device_id: str, attrs: dict, source_fix: DeviceFix | LocationFix):
        if hasattr(self.mqtt, "publish_location"):
            self.mqtt.publish_location(device_id, attrs)
            return

        if isinstance(source_fix, DeviceFix) and hasattr(self.mqtt, "publish_device_fix"):
            compat_fix = DeviceFix(
                identifier=device_id,
                name=source_fix.name,
                model=source_fix.model,
                latitude=attrs["latitude"],
                longitude=attrs["longitude"],
                horizontal_accuracy=attrs["gps_accuracy"],
                timestamp_unix=attrs["last_seen"],
                battery_level=source_fix.battery_level,
                battery_status=source_fix.battery_status,
                device_class=source_fix.device_class,
                ba_uuid=source_fix.ba_uuid,
                owner=source_fix.owner,
            )
            self.mqtt.publish_device_fix(compat_fix)
            return

        if hasattr(self.mqtt, "publish_fix"):
            compat_fix = LocationFix(
                identifier=device_id,
                name=source_fix.name,
                model=source_fix.model,
                latitude=attrs["latitude"],
                longitude=attrs["longitude"],
                horizontal_accuracy=attrs["gps_accuracy"],
                timestamp_unix=attrs["last_seen"],
                shared_by=attrs.get("owner"),
                shared_date=attrs.get("shared_date"),
            )
            self.mqtt.publish_fix(compat_fix)

    def _publish_battery(self, device_id: str, percentage: int):
        if hasattr(self.mqtt, "publish_battery"):
            self.mqtt.publish_battery(device_id, percentage)

    def _set_availability(self, device_id: str, available: bool, force: bool = False):
        previous = self._availability.get(device_id)
        self._availability[device_id] = available

        if hasattr(self.mqtt, "publish_device_availability"):
            if force or previous != available:
                self.mqtt.publish_device_availability(device_id, available)
            return

        if not available and previous is not False and hasattr(self.mqtt, "publish_unavailable"):
            self.mqtt.publish_unavailable(device_id)

    def _create_loop_task(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=f"presencesync-{name}")
        task.add_done_callback(self._log_background_error)
        return task

    def _schedule(self, coro):
        """Schedule a coroutine from any thread (e.g. paho MQTT callbacks)."""
        loop = self._loop
        if loop is None:
            log.error("Cannot schedule coroutine: event loop not set")
            coro.close()
            return
        loop.call_soon_threadsafe(self._create_tracked_task, coro)

    def _create_tracked_task(self, coro):
        task = asyncio.ensure_future(coro)
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
        task.add_done_callback(self._log_background_error)

    @staticmethod
    def _log_background_error(task: asyncio.Task):
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.exception("Background task failed", exc_info=exc)


_coord: Coordinator | None = None


def get() -> Coordinator:
    global _coord
    if _coord is None:
        _coord = Coordinator()
    return _coord
