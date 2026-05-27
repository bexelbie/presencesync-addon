"""Tests for coordinator stationary denoising and availability logic."""
import json
from unittest.mock import MagicMock

from presencesync.coordinator import _haversine_m, Coordinator, KNOWN_DEVICES_PATH


class TestHaversine:
    def test_same_point(self):
        assert _haversine_m(0, 0, 0, 0) == 0.0

    def test_known_distance(self):
        d = _haversine_m(40.7128, -74.0060, 34.0522, -118.2437)
        assert 3_900_000 < d < 4_000_000


class TestStationary:
    def test_first_report_sets_anchor(self, mock_state):
        coord = Coordinator.__new__(Coordinator)
        coord._anchors = {}

        from presencesync import identity, state

        store = identity.DeviceIdentityStore(path=state.DEVICE_IDENTITY_PATH)
        device_id = store.register("test-raw-id", "Test", "fmip")

        lat, lon = coord._apply_stationary(device_id, 49.0, 16.0)

        assert (lat, lon) == (49.0, 16.0)
        assert coord._anchors[device_id] == (49.0, 16.0)

    def test_within_radius_pins_to_anchor(self, mock_state):
        coord = Coordinator.__new__(Coordinator)
        coord._anchors = {"abc123": (49.0, 16.0)}

        lat, lon = coord._apply_stationary("abc123", 49.00009, 16.00009)

        assert (lat, lon) == (49.0, 16.0)

    def test_beyond_radius_updates_anchor(self, mock_state):
        coord = Coordinator.__new__(Coordinator)
        coord._anchors = {"abc123": (49.0, 16.0)}

        lat, lon = coord._apply_stationary("abc123", 49.005, 16.005)

        assert (lat, lon) == (49.005, 16.005)
        assert coord._anchors["abc123"] == (49.005, 16.005)


class TestKnownDevicesCache:
    def test_load_populates_previous_sets(self, mock_state):
        from presencesync import state as st

        KNOWN_DEVICES_PATH_TEST = st.DATA_DIR / "known_devices.json"
        KNOWN_DEVICES_PATH_TEST.write_text(json.dumps({
            "idevices": ["dev_a", "dev_b"],
            "items": ["item_x"],
        }))

        coord = Coordinator.__new__(Coordinator)
        coord._prev_idevice_ids = set()
        coord._prev_item_ids = set()
        coord._load_known_devices()

        assert coord._prev_idevice_ids == {"dev_a", "dev_b"}
        assert coord._prev_item_ids == {"item_x"}

    def test_load_missing_file_leaves_empty_sets(self, mock_state):
        coord = Coordinator.__new__(Coordinator)
        coord._prev_idevice_ids = set()
        coord._prev_item_ids = set()
        coord._load_known_devices()

        assert coord._prev_idevice_ids == set()
        assert coord._prev_item_ids == set()

    def test_save_persists_current_sets(self, mock_state):
        from presencesync import state as st

        coord = Coordinator.__new__(Coordinator)
        coord._prev_idevice_ids = {"dev_a", "dev_b"}
        coord._prev_item_ids = {"item_x"}
        coord._save_known_devices()

        data = json.loads((st.DATA_DIR / "known_devices.json").read_text())
        assert set(data["idevices"]) == {"dev_a", "dev_b"}
        assert set(data["items"]) == {"item_x"}


class TestIDeviceAvailability:
    def _make_coord(self, mock_state):
        from presencesync import identity, state

        coord = Coordinator.__new__(Coordinator)
        coord._prev_idevice_ids = set()
        coord._prev_item_ids = set()
        coord._availability = {}
        coord.mqtt = MagicMock()
        coord.mqtt.publish_device_availability = MagicMock()
        return coord

    def test_first_poll_marks_devices_online(self, mock_state, make_fix):
        coord = self._make_coord(mock_state)
        fixes = [make_fix(identifier="phone-1", name="iPhone")]

        coord._update_idevice_availability(fixes)

        from presencesync import identity
        store = identity.get()
        device_id = store.get_hash("phone-1")
        assert coord._availability[device_id] is True

    def test_disappeared_device_marked_offline(self, mock_state, make_fix):
        from presencesync import identity
        coord = self._make_coord(mock_state)

        # First poll: phone-1 and phone-2
        fixes = [
            make_fix(identifier="phone-1", name="iPhone"),
            make_fix(identifier="phone-2", name="iPad"),
        ]
        coord._update_idevice_availability(fixes)

        store = identity.get()
        phone2_id = store.get_hash("phone-2")

        # Second poll: only phone-1
        fixes = [make_fix(identifier="phone-1", name="iPhone")]
        coord._update_idevice_availability(fixes)

        assert coord._availability[phone2_id] is False

    def test_excluded_device_not_tracked(self, mock_state, make_fix):
        from presencesync import identity, state

        coord = self._make_coord(mock_state)
        store = identity.DeviceIdentityStore(path=state.DEVICE_IDENTITY_PATH)
        device_id = store.register("phone-1", "iPhone", "fmip")

        mock_state.OPTIONS_PATH.write_text(json.dumps({
            "devices": [{"id": device_id, "exclude": True}],
        }))
        state.reload_addon_config()

        fixes = [make_fix(identifier="phone-1", name="iPhone")]
        coord._update_idevice_availability(fixes)

        assert device_id not in coord._prev_idevice_ids

    def test_cached_prev_set_detects_disappearance_on_first_poll(self, mock_state, make_fix):
        """If cached data says phone-2 existed, first poll without it marks offline."""
        from presencesync import identity
        coord = self._make_coord(mock_state)
        store = identity.DeviceIdentityStore()
        phone2_id = store.register("phone-2", "iPad", "fmip")

        coord._prev_idevice_ids = {phone2_id}

        fixes = [make_fix(identifier="phone-1", name="iPhone")]
        coord._update_idevice_availability(fixes)

        assert coord._availability[phone2_id] is False

    def test_fresh_install_no_false_offlines(self, mock_state, make_fix):
        """First poll with empty cache never marks anything offline."""
        coord = self._make_coord(mock_state)
        coord._prev_idevice_ids = set()  # fresh install

        fixes = [make_fix(identifier="phone-1", name="iPhone")]
        coord._update_idevice_availability(fixes)

        # Only online transitions, no offline
        from presencesync import identity
        store = identity.get()
        phone1_id = store.get_hash("phone-1")
        assert coord._availability[phone1_id] is True
        assert all(v is True for v in coord._availability.values())
