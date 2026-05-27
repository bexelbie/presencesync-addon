"""Tests for web API endpoints."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def coord_mock():
    """Create a mock coordinator for web tests."""
    coord = MagicMock()
    coord.apple = MagicMock()
    coord.apple.last_login_state = "LOGGED_IN"
    coord.apple.accessories = []
    coord.apple.shared_accessories = []
    coord.apple.account = None
    coord.apple.anisette = None
    coord.apple.ensure_account = AsyncMock()
    coord.apple.login = AsyncMock(return_value="LOGGED_IN")
    coord.apple.load_keys_dir = MagicMock()
    coord.icloud = MagicMock()
    coord.icloud.login_state = "logged_in"
    coord.icloud.login = MagicMock(return_value="logged_in")
    coord.icloud.reset = MagicMock()
    coord.mqtt = MagicMock()
    coord.mqtt.connected = True
    coord.last_run_unix = 0
    coord._icloud_consecutive_failures = 0
    coord._stop_event = asyncio.Event()
    coord._poll_task = None
    coord._refresh_task = None
    coord._item_task = None
    coord._create_loop_task = MagicMock(return_value=MagicMock())
    coord._poll_loop = MagicMock(return_value=AsyncMock()())
    coord._refresh_loop = MagicMock(return_value=AsyncMock()())
    coord._item_loop = MagicMock(return_value=AsyncMock()())
    coord.start = AsyncMock()
    coord.stop = AsyncMock()
    return coord


@pytest.fixture
def client(mock_state, coord_mock):
    """Create a test client with mocked coordinator and lifespan."""
    with patch("presencesync.web.get_coord", return_value=coord_mock), \
         patch("presencesync.web._auto_configure", new_callable=AsyncMock), \
         patch("presencesync.web.supervisor") as mock_sup, \
         patch("presencesync.web.anisette_manager") as mock_anisette:
        mock_sup.dismiss_notification = AsyncMock()
        mock_anisette.get.return_value = MagicMock(running=True)

        from presencesync.web import app
        with TestClient(app) as c:
            yield c, coord_mock, mock_sup


class TestStatusEndpoint:
    def test_returns_status(self, client):
        c, coord, _ = client
        # Need to mock __version__
        with patch("presencesync.__version__", "1.0.0"):
            extractor_mock = MagicMock()
            extractor_mock.available = True
            with patch("presencesync.web._extractor_mod") as ext:
                ext.get.return_value = extractor_mock
                resp = c.get("/api/status")

        assert resp.status_code == 200
        data = resp.json()
        assert "icloud_login_state" in data
        assert "mqtt_connected" in data


class TestLoginEndpoint:
    def test_login_success(self, client):
        c, coord, mock_sup = client
        resp = c.post("/api/apple/login", json={
            "username": "user@example.com",
            "password": "pass123",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["icloud_state"] == "logged_in"

    def test_login_missing_credentials(self, client):
        c, coord, _ = client
        resp = c.post("/api/apple/login", json={
            "username": "",
            "password": "",
        })
        assert resp.status_code == 400

    def test_login_success_dismisses_notification(self, client):
        c, coord, mock_sup = client
        resp = c.post("/api/apple/login", json={
            "username": "user@example.com",
            "password": "pass123",
        })

        assert resp.status_code == 200
        mock_sup.dismiss_notification.assert_called_with("presencesync_reauth_required")

    def test_login_success_restarts_stopped_loops(self, client):
        c, coord, _ = client
        coord._stop_event.set()  # simulate stopped state

        resp = c.post("/api/apple/login", json={
            "username": "user@example.com",
            "password": "pass123",
        })

        assert resp.status_code == 200
        assert not coord._stop_event.is_set()
        coord._create_loop_task.assert_called()

    def test_login_failure_does_not_restart(self, client):
        c, coord, _ = client
        coord.icloud.login = MagicMock(return_value="needs_2fa")
        coord._stop_event.set()

        resp = c.post("/api/apple/login", json={
            "username": "user@example.com",
            "password": "pass123",
        })

        assert resp.status_code == 200
        assert coord._stop_event.is_set()  # still stopped


class TestResetEndpoint:
    def test_reset_clears_state(self, client):
        c, coord, _ = client
        resp = c.post("/api/reset")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        coord.icloud.reset.assert_called_once()
        assert coord.apple.accessories == []
        assert coord.apple.shared_accessories == []

    def test_reset_clears_credentials(self, mock_state, client):
        from presencesync import state
        c, coord, _ = client

        # Set credentials first
        settings = state.get()
        settings.apple.username = "user@example.com"
        settings.apple.password = "secret"

        resp = c.post("/api/reset")
        assert resp.status_code == 200

        settings = state.get()
        assert settings.apple.username == ""
        assert settings.apple.password == ""


class TestSubmit2FA:
    def test_submit_2fa_requires_code(self, client):
        c, coord, _ = client
        resp = c.post("/api/apple/2fa/submit", json={"code": ""})
        assert resp.status_code == 400

    def test_submit_2fa_passes_code_to_backends(self, client):
        c, coord, _ = client
        coord.icloud.login_state = "needs_2fa"
        coord.icloud.submit_2fa = MagicMock(return_value="logged_in")
        coord.apple.last_login_state = "REQUIRE_2FA"
        coord.apple.submit_2fa = AsyncMock(return_value="LOGGED_IN")

        resp = c.post("/api/apple/2fa/submit", json={"code": "123456"})

        assert resp.status_code == 200
        coord.icloud.submit_2fa.assert_called_once_with("123456")
