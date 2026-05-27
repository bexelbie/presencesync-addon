"""Tests for supervisor notification functions."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aiohttp import ClientTimeout

from presencesync import supervisor


class TestCreateNotification:
    @pytest.mark.asyncio
    async def test_creates_notification_successfully(self, mock_state, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await supervisor.create_notification(
                "test_id", "Test Title", "Test message"
            )

        assert result is True
        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert "persistent_notification/create" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["title"] == "Test Title"
        assert payload["message"] == "Test message"
        assert payload["notification_id"] == "test_id"

    @pytest.mark.asyncio
    async def test_returns_false_without_token(self, mock_state, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)

        result = await supervisor.create_notification("id", "title", "msg")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_http_error(self, mock_state, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Internal Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await supervisor.create_notification("id", "title", "msg")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self, mock_state, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        with patch("aiohttp.ClientSession", side_effect=Exception("connection refused")):
            result = await supervisor.create_notification("id", "title", "msg")

        assert result is False


class TestDismissNotification:
    @pytest.mark.asyncio
    async def test_dismisses_successfully(self, mock_state, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await supervisor.dismiss_notification("test_id")

        assert result is True
        call_kwargs = mock_session.post.call_args
        assert "persistent_notification/dismiss" in call_kwargs[0][0]
        payload = call_kwargs[1]["json"]
        assert payload["notification_id"] == "test_id"

    @pytest.mark.asyncio
    async def test_returns_false_without_token(self, mock_state, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HASSIO_TOKEN", raising=False)

        result = await supervisor.dismiss_notification("id")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_failure(self, mock_state, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await supervisor.dismiss_notification("id")

        assert result is False
