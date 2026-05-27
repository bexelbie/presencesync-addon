"""Tests for coordinator device processing, dedup, and auth failure logic."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from presencesync.apple import LocationFix
from presencesync.coordinator import Coordinator


def _make_coord(mock_state):
    """Build a minimally-initialized Coordinator for unit testing."""
    coord = Coordinator.__new__(Coordinator)
    coord._published_timestamps = {}
    coord._anchors = {}
    coord._ck_to_stable = {}
    coord._fmi_ts_by_beacon_id = {}
    coord._fmi_id_by_beacon_id = {}
    coord._raw_icloud_ids_by_device_id = {}
    coord._prev_idevice_ids = set()
    coord._prev_item_ids = set()
    coord._availability = {}
    coord._icloud_consecutive_failures = 0
    coord._stop_event = asyncio.Event()
    coord._poll_task = None
    coord._refresh_task = None
    coord._item_task = None
    coord.mqtt = MagicMock()
    coord.mqtt.publish_device_availability = MagicMock()
    coord.icloud = MagicMock()
    return coord


def _make_location_fix(
    identifier="item-1",
    name="Keys",
    model="AirTag",
    latitude=37.7749,
    longitude=-122.4194,
    horizontal_accuracy=5.0,
    timestamp_unix=1700000000,
    shared_by=None,
    shared_date=None,
):
    return LocationFix(
        identifier=identifier,
        name=name,
        model=model,
        latitude=latitude,
        longitude=longitude,
        horizontal_accuracy=horizontal_accuracy,
        timestamp_unix=timestamp_unix,
        shared_by=shared_by,
        shared_date=shared_date,
    )


class TestProcessIDevice:
    """Tests for _process_idevice: publish, dedup, exclusion."""

    @pytest.mark.asyncio
    async def test_publishes_location_and_updates_timestamp(self, mock_state, make_fix):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()

        fix = make_fix(identifier="phone-1", name="iPhone", timestamp_unix=1700000000)
        await coord._process_idevice(fix)

        from presencesync import identity
        store = identity.get()
        device_id = store.get_hash("phone-1")

        coord._publish_location.assert_called_once()
        coord._publish_device_discovery.assert_called_once()
        assert coord._published_timestamps[device_id] == 1700000000

    @pytest.mark.asyncio
    async def test_dedup_skips_stale_timestamp(self, mock_state, make_fix):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()

        fix = make_fix(identifier="phone-1", timestamp_unix=1700000000)
        await coord._process_idevice(fix)

        # Same timestamp — should be deduped
        coord._publish_location.reset_mock()
        await coord._process_idevice(fix)
        coord._publish_location.assert_not_called()

    @pytest.mark.asyncio
    async def test_newer_timestamp_publishes(self, mock_state, make_fix):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()

        fix1 = make_fix(identifier="phone-1", timestamp_unix=1700000000)
        fix2 = make_fix(identifier="phone-1", timestamp_unix=1700000001)
        await coord._process_idevice(fix1)
        coord._publish_location.reset_mock()
        await coord._process_idevice(fix2)
        coord._publish_location.assert_called_once()

    @pytest.mark.asyncio
    async def test_excluded_device_skipped(self, mock_state, make_fix):
        from presencesync import identity, state

        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()

        # Register the device first to get its ID
        store = identity.DeviceIdentityStore(path=state.DEVICE_IDENTITY_PATH)
        device_id = store.register("phone-1", "iPhone", "fmip")

        # Exclude it via config
        mock_state.OPTIONS_PATH.write_text(json.dumps({
            "devices": [{"id": device_id, "exclude": True}],
        }))
        state.reload_addon_config()

        fix = make_fix(identifier="phone-1", name="iPhone")
        await coord._process_idevice(fix)
        coord._publish_location.assert_not_called()

    @pytest.mark.asyncio
    async def test_battery_published_when_present(self, mock_state, make_fix):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()

        fix = make_fix(identifier="phone-1", battery_level=0.75)
        await coord._process_idevice(fix)
        coord._publish_battery.assert_called_once_with(
            coord._publish_battery.call_args[0][0], 75
        )

    @pytest.mark.asyncio
    async def test_no_battery_when_none(self, mock_state, make_fix):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()

        fix = make_fix(identifier="phone-1", battery_level=None)
        await coord._process_idevice(fix)
        coord._publish_battery.assert_not_called()

    @pytest.mark.asyncio
    async def test_beacon_correlation_tracking(self, mock_state, make_fix):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._publish_battery = MagicMock()
        coord._ck_to_stable = {"beacon-uuid-1": "l:/stable-id"}

        fix = make_fix(identifier="phone-1", ba_uuid="beacon-uuid-1", timestamp_unix=1700000000)
        await coord._process_idevice(fix)

        assert coord._fmi_ts_by_beacon_id["l:/stable-id"] == 1700000000
        assert coord._fmi_id_by_beacon_id["l:/stable-id"] == "phone-1"


class TestProcessItem:
    """Tests for _process_item: items, beacons, dedup."""

    @pytest.mark.asyncio
    async def test_publishes_item_location(self, mock_state):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()

        fix = _make_location_fix(identifier="item-1", name="Keys")
        await coord._process_item(fix)

        coord._publish_location.assert_called_once()
        coord._publish_device_discovery.assert_called_once()

    @pytest.mark.asyncio
    async def test_item_dedup_skips_stale(self, mock_state):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()

        fix = _make_location_fix(identifier="item-1", timestamp_unix=1700000000)
        await coord._process_item(fix)

        coord._publish_location.reset_mock()
        await coord._process_item(fix)
        coord._publish_location.assert_not_called()

    @pytest.mark.asyncio
    async def test_idevice_beacon_skipped_when_fmip_fresher(self, mock_state):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._fmi_ts_by_beacon_id = {"l:/beacon-1": 1700000010}
        coord._fmi_id_by_beacon_id = {"l:/beacon-1": "phone-1"}

        # Beacon report older than FMiP data — should be skipped
        fix = _make_location_fix(identifier="l:/beacon-1", timestamp_unix=1700000005)
        await coord._process_item(fix)
        coord._publish_location.assert_not_called()

    @pytest.mark.asyncio
    async def test_idevice_beacon_publishes_when_fresher_than_fmip(self, mock_state):
        from presencesync import identity

        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        coord._fmi_ts_by_beacon_id = {"l:/beacon-1": 1700000000}
        coord._fmi_id_by_beacon_id = {"l:/beacon-1": "phone-1"}

        # Register the FMiP device so get_hash works
        store = identity.get()
        store.register("phone-1", "iPhone", "fmip")

        fix = _make_location_fix(identifier="l:/beacon-1", timestamp_unix=1700000015)
        await coord._process_item(fix)
        coord._publish_location.assert_called_once()

    @pytest.mark.asyncio
    async def test_idevice_beacon_skipped_when_no_fmip_correlation(self, mock_state):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()
        # No entry in _fmi_id_by_beacon_id for this beacon
        coord._fmi_ts_by_beacon_id = {"l:/beacon-1": 0}

        fix = _make_location_fix(identifier="l:/beacon-1", timestamp_unix=1700000015)
        await coord._process_item(fix)
        coord._publish_location.assert_not_called()

    @pytest.mark.asyncio
    async def test_shared_item_includes_owner(self, mock_state):
        coord = _make_coord(mock_state)
        coord._publish_device_discovery = MagicMock()
        coord._publish_location = MagicMock()

        fix = _make_location_fix(
            identifier="shared-1", name="Bag", shared_by="alex@example.com"
        )
        await coord._process_item(fix)

        call_args = coord._publish_location.call_args[0]
        attrs = call_args[1]
        assert attrs["owner"] == "alex@example.com"


class TestAuthFailure:
    """Tests for _handle_icloud_failure and _enter_auth_failed_state."""

    @pytest.mark.asyncio
    async def test_no_action_below_threshold(self, mock_state):
        coord = _make_coord(mock_state)
        coord._icloud_consecutive_failures = 0

        with patch("presencesync.coordinator.log"):
            # First two failures: no recovery attempted
            try:
                raise RuntimeError("simulated")
            except Exception:
                await coord._handle_icloud_failure("poll")
                await coord._handle_icloud_failure("poll")

        assert coord._icloud_consecutive_failures == 2
        assert not coord._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_recovery_succeeds_resets_counter(self, mock_state):
        from presencesync import state as st

        coord = _make_coord(mock_state)
        coord._icloud_consecutive_failures = 2

        # Store credentials so recovery is attempted
        settings = st.get()
        settings.apple.username = "user@example.com"
        settings.apple.password = "pass123"

        coord.icloud.login = MagicMock(return_value="logged_in")

        with patch("presencesync.coordinator.supervisor.dismiss_notification", new_callable=AsyncMock):
            try:
                raise RuntimeError("simulated")
            except Exception:
                await coord._handle_icloud_failure("poll")

        assert coord._icloud_consecutive_failures == 0
        assert not coord._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_recovery_fails_stops_loops_and_notifies(self, mock_state):
        from presencesync import state as st

        coord = _make_coord(mock_state)
        coord._icloud_consecutive_failures = 2

        settings = st.get()
        settings.apple.username = "user@example.com"
        settings.apple.password = "pass123"

        coord.icloud.login = MagicMock(return_value="requires_2fa")

        with patch("presencesync.coordinator.supervisor.create_notification", new_callable=AsyncMock) as mock_notify:
            try:
                raise RuntimeError("simulated")
            except Exception:
                await coord._handle_icloud_failure("poll")

        assert coord._stop_event.is_set()
        mock_notify.assert_called_once_with(
            "presencesync_reauth_required",
            "PresenceSync: Re-authentication required",
            "Apple session expired and automatic recovery failed. "
            "Open the PresenceSync add-on and log in again to resume location tracking.",
        )

    @pytest.mark.asyncio
    async def test_recovery_exception_stops_loops(self, mock_state):
        from presencesync import state as st

        coord = _make_coord(mock_state)
        coord._icloud_consecutive_failures = 2

        settings = st.get()
        settings.apple.username = "user@example.com"
        settings.apple.password = "pass123"

        coord.icloud.login = MagicMock(side_effect=Exception("network error"))

        with patch("presencesync.coordinator.supervisor.create_notification", new_callable=AsyncMock):
            try:
                raise RuntimeError("simulated")
            except Exception:
                await coord._handle_icloud_failure("poll")

        assert coord._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_no_credentials_stops_loops(self, mock_state):
        coord = _make_coord(mock_state)
        coord._icloud_consecutive_failures = 2

        with patch("presencesync.coordinator.supervisor.create_notification", new_callable=AsyncMock) as mock_notify:
            try:
                raise RuntimeError("simulated")
            except Exception:
                await coord._handle_icloud_failure("poll")

        assert coord._stop_event.is_set()
        mock_notify.assert_called_once()
