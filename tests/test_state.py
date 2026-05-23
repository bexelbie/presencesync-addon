"""Tests for state module — config loading and persistence."""
from __future__ import annotations

import json
import pytest


class TestTrackingConfigDefaults:
    """Verify all new config fields have proper defaults."""

    def test_smart_tracking_defaults(self, mock_state):
        cfg = mock_state._current.tracking
        assert cfg.dynamic_polling is True
        assert cfg.flap_suppression_count == 3
        assert cfg.stationary_threshold_minutes == 15
        assert cfg.battery_low_threshold == 0.20
        assert cfg.battery_critical_threshold == 0.10
        assert cfg.stale_threshold_hours == 4.0

    def test_airtag_defaults(self, mock_state):
        cfg = mock_state._current.tracking
        assert cfg.airtag_poll_interval_s == 600
        assert cfg.airtag_movement_interval_s == 300
        assert cfg.airtag_movement_threshold_m == 200.0


class TestStatePersistence:
    """Config save/load round-trip."""

    def test_save_and_load(self, mock_state, tmp_path):
        from presencesync.state import Settings, CONFIG_PATH

        mock_state._current.tracking.dynamic_polling = False
        mock_state._current.tracking.flap_suppression_count = 5
        mock_state._current.home.latitude = 37.7749
        mock_state._current.home.longitude = -122.4194

        # Save
        from dataclasses import asdict
        config_path = tmp_path / "presencesync.json"
        config_path.write_text(json.dumps(asdict(mock_state._current)))

        # Load
        raw = json.loads(config_path.read_text())
        loaded = Settings(
            tracking=mock_state.TrackingConfig(**raw["tracking"]),
            home=mock_state.HomeLocation(**raw["home"]),
        )

        assert loaded.tracking.dynamic_polling is False
        assert loaded.tracking.flap_suppression_count == 5
        assert loaded.home.latitude == 37.7749
