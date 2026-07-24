"""Tests for Portal Market client."""

from __future__ import annotations

from bot.portal_client import PortalClient


class TestPortalClientInit:
    def test_unauthenticated(self) -> None:
        client = PortalClient()
        assert not client.ready
        assert not client.authenticated

    def test_authenticated(self) -> None:
        client = PortalClient(tma_init_data="test_data")
        assert client.ready
        assert client.authenticated

    def test_update_auth(self) -> None:
        client = PortalClient()
        assert not client.ready
        client._tma_init_data = "new_data"
        assert client.ready


class TestPortalClientCacheMapping:
    def test_collection_id_mapping(self) -> None:
        client = PortalClient()
        client._collection_ids["Test Collection"] = "uuid-123"
        client._id_to_name["uuid-123"] = "Test Collection"

        assert client.get_collection_id("Test Collection") == "uuid-123"
        assert client.get_collection_name("uuid-123") == "Test Collection"
        assert client.get_collection_id("Unknown") == ""
        assert client.get_collection_name("unknown-uuid") == ""

    def test_cached_floors(self) -> None:
        client = PortalClient()
        client._cached_floors["Gift A"] = 10.5
        client._cached_floors["Gift B"] = 0.0

        assert client._cached_floors["Gift A"] == 10.5
        assert client._cached_floors["Gift B"] == 0.0
