"""Tests for coordinator stationary denoising helpers."""
from presencesync.coordinator import _haversine_m, Coordinator


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
