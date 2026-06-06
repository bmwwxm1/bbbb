"""Tests for MRKTClient — auth retry, token refresh, session management."""

from __future__ import annotations

import pytest

from bot.mrkt_client import MRKTClient


class TestMrktClientInit:
    def test_default_attributes(self):
        client = MRKTClient()
        assert client._token_alert_sent is False
        assert client._session_dirty is False
        assert client._session is None

    def test_update_token(self):
        client = MRKTClient()
        client.update_token("new-token-123")
        assert client._auth_token == "new-token-123"
        assert client._session_dirty is True
        assert client._token_alert_sent is False

    def test_multiple_token_updates(self):
        client = MRKTClient()
        client._token_alert_sent = True
        client.update_token("token-2")
        assert client._token_alert_sent is False
        client._token_alert_sent = True
        client.update_token("token-3")
        assert client._token_alert_sent is False


class TestMrktClientSession:
    @pytest.mark.asyncio
    async def test_close_no_session(self):
        client = MRKTClient()
        await client.close()  # should not raise


class TestMrktClientTokenCallback:
    def test_callback_set(self):
        client = MRKTClient()
        assert client._on_token_expired is None

        async def cb():
            pass

        client.set_token_expired_callback(cb)
        assert client._on_token_expired is cb
