"""Tests for the tracker module — per-device intelligence."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest


class TestHaversine:
    """Distance calculation."""

    def test_same_point_is_zero(self):
        from presencesync.tracker import _haversine_m
        assert _haversine_m(37.7749, -122.4194, 37.7749, -122.4194) == 0.0

    def test_known_distance(self):
        """SF to Oakland is ~13 km."""
        from presencesync.tracker import _haversine_m
        d = _haversine_m(37.7749, -122.4194, 37.8044, -122.2712)
        assert 12_000 < d < 15_000

    def test_short_distance(self):
        """Two points ~100m apart."""
        from presencesync.tracker import _haversine_m
        # ~111m per 0.001 degree latitude
        d = _haversine_m(37.7749, -122.4194, 37.7759, -122.4194)
        assert 100 < d < 120


class TestDeviceTracker:
    """Core per-device state machine."""

    def test_first_fix_publishes(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker, PublishAction

        # Set home location so "home" determination works
        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        fix = make_fix(latitude=37.7749, longitude=-122.4194)  # at home
        decision = dt.ingest(fix)

        assert decision.action == PublishAction.PUBLISH
        assert decision.state == "home"

    def test_not_home_when_far(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker, PublishAction

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        # Oakland — well outside 100m radius
        fix = make_fix(latitude=37.8044, longitude=-122.2712)
        decision = dt.ingest(fix)

        assert decision.action == PublishAction.PUBLISH
        assert decision.state == "not_home"

    def test_flap_suppression_blocks_premature_flip(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker, PublishAction

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194
        mock_state._current.tracking.flap_suppression_count = 3

        dt = DeviceTracker("dev1", "Test iPhone")

        # First: at home
        dt.ingest(make_fix(latitude=37.7749, longitude=-122.4194))
        assert dt.last_published_state == "home"

        # Now one reading far away — should suppress
        decision = dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712))
        assert decision.action == PublishAction.SUPPRESS
        assert dt.last_published_state == "home"  # still home

        # Second far reading — still suppressed (need 3)
        decision = dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712))
        assert decision.action == PublishAction.SUPPRESS
        assert dt.last_published_state == "home"

        # Third — should flip
        decision = dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712))
        assert decision.action == PublishAction.PUBLISH
        assert decision.state == "not_home"
        assert dt.last_published_state == "not_home"

    def test_flap_suppression_resets_on_back_to_same(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker, PublishAction

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194
        mock_state._current.tracking.flap_suppression_count = 3

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix(latitude=37.7749, longitude=-122.4194))  # home

        # One far reading
        dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712))
        # Then back home — counter should reset
        decision = dt.ingest(make_fix(latitude=37.7749, longitude=-122.4194))
        assert decision.action == PublishAction.PUBLISH
        assert decision.state == "home"

    def test_dynamic_polling_disabled_always_publishes(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker, PublishAction

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194
        mock_state._current.tracking.dynamic_polling = False

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix(latitude=37.7749, longitude=-122.4194))  # home
        # Immediately far — no flap suppression
        decision = dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712))
        assert decision.action == PublishAction.PUBLISH
        assert decision.state == "not_home"

    def test_is_stale_after_threshold(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.tracking.stale_threshold_hours = 4.0  # 4h = 14400s, but min is 600s

        dt = DeviceTracker("dev1", "Test iPhone")
        # Ingest a fix, then wind the clock forward past the minimum (600s)
        dt.ingest(make_fix())
        dt.last_fix_time = time.time() - 15000  # 15000s ago (> 14400s threshold)
        assert dt.is_stale()

    def test_is_not_stale_when_recent(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix())
        assert not dt.is_stale()

    def test_never_seen_is_not_stale(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        dt = DeviceTracker("dev1", "Test iPhone")
        # No ingest — last_fix_time is 0
        assert not dt.is_stale()


class TestDynamicPolling:
    """next_poll_seconds behavior."""

    def test_close_distance_short_interval(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        # 200m away (within 500m bracket)
        dt.ingest(make_fix(latitude=37.77672, longitude=-122.4194))
        interval = dt.next_poll_seconds()
        assert interval == 30

    def test_far_distance_long_interval(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        # 13km away (> 5km bracket)
        dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712))
        interval = dt.next_poll_seconds()
        assert interval == 300

    def test_battery_low_doubles_interval(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712,
                           battery_level=0.15, battery_status="Unplugged"))
        interval = dt.next_poll_seconds()
        # 300 * 2 = 600, but clamped to max 900
        assert interval == 600

    def test_battery_critical_quadruples_interval(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712,
                           battery_level=0.05, battery_status="Unplugged"))
        interval = dt.next_poll_seconds()
        # 300 * 4 = 1200, clamped to 900
        assert interval == 900

    def test_battery_charging_no_penalty(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix(latitude=37.8044, longitude=-122.2712,
                           battery_level=0.05, battery_status="Charging"))
        interval = dt.next_poll_seconds()
        # No battery penalty because charging
        assert interval == 300

    def test_stationary_extends_interval(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194
        mock_state._current.tracking.stationary_threshold_minutes = 15

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix(latitude=37.77672, longitude=-122.4194))  # 200m = normally 30s
        # Pretend no movement for 20 minutes
        dt.last_movement_time = time.time() - (20 * 60)
        interval = dt.next_poll_seconds()
        assert interval >= 300  # stationary bumps to 5min minimum

    def test_disabled_returns_base_interval(self, mock_state, make_fix):
        from presencesync.tracker import DeviceTracker

        mock_state._current.tracking.dynamic_polling = False
        mock_state._current.tracking.poll_interval_s = 120

        dt = DeviceTracker("dev1", "Test iPhone")
        dt.ingest(make_fix())
        assert dt.next_poll_seconds() == 120


class TestTrackerManager:
    """Manager-level coordination."""

    def test_creates_tracker_on_first_ingest(self, mock_state, make_fix):
        from presencesync.tracker import TrackerManager, PublishAction

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        mgr = TrackerManager()
        decision = mgr.ingest(make_fix(identifier="dev-abc"))
        assert decision.action == PublishAction.PUBLISH
        assert "dev-abc" in mgr._trackers

    def test_next_poll_uses_minimum(self, mock_state, make_fix):
        from presencesync.tracker import TrackerManager

        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        mgr = TrackerManager()
        # Close device (30s)
        mgr.ingest(make_fix(identifier="close", latitude=37.77672, longitude=-122.4194))
        # Far device (300s)
        mgr.ingest(make_fix(identifier="far", latitude=37.8044, longitude=-122.2712))

        assert mgr.next_poll_seconds() == 30  # minimum wins

    def test_stale_devices_empty_when_recent(self, mock_state, make_fix):
        from presencesync.tracker import TrackerManager

        mgr = TrackerManager()
        mgr.ingest(make_fix())
        assert mgr.stale_devices() == []

    def test_stale_devices_detected(self, mock_state, make_fix):
        from presencesync.tracker import TrackerManager

        mock_state._current.tracking.stale_threshold_hours = 4.0

        mgr = TrackerManager()
        mgr.ingest(make_fix(identifier="stale-dev"))
        # Wind clock past 4h threshold
        mgr._trackers["stale-dev"].last_fix_time = time.time() - 15000
        stale = mgr.stale_devices()
        assert len(stale) == 1
        assert stale[0].device_id == "stale-dev"
