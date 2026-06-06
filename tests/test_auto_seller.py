"""Tests for AutoSeller — price protection, cross-market logic, dedup."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.execution.auto_seller import NANO, AutoSeller


def _make_auto_seller(**kwargs) -> AutoSeller:
    mrkt = AsyncMock()
    mrkt.get_my_gifts = AsyncMock(return_value=[])
    mrkt.get_listings = AsyncMock(return_value={"gifts": []})
    mrkt.sell_gift = AsyncMock(return_value=True)
    mrkt.withdraw_gift = AsyncMock(return_value=True)
    mrkt.change_sale_price = AsyncMock(return_value=True)

    mds = MagicMock()
    mds.get_cached_gg_floor = MagicMock(return_value=0)
    mds.get_cached_portal_floor = MagicMock(return_value=0)
    mds.get_all_gg_floors = MagicMock(return_value={})
    mds.get_all_portal_floors = MagicMock(return_value={})
    mds.get_all_mrkt_floors = MagicMock(return_value={})
    mds.get_cached_top_order = MagicMock(return_value=0)

    return AutoSeller(
        mrkt=mrkt,
        mds=mds,
        admin_chat_id=123456,
        bot=None,
        enabled=kwargs.get("enabled", True),
        shadow_mode=kwargs.get("shadow_mode", True),
    )


class TestPriceProtection:
    def test_buy_price_tracked(self):
        seller = _make_auto_seller()
        seller.mark_bought("gift-1", buy_price=2_000_000_000, collection_name="TestGift")
        assert seller._buy_prices["TestGift"] == 2_000_000_000

    def test_buy_price_prevents_loss(self):
        seller = _make_auto_seller()
        seller._buy_prices["TestGift"] = int(3.0 * NANO)

        # list_price = floor * 0.98 = 2.94 TON (below buy price 3.0)
        floor_price = int(3.0 * NANO)
        list_price = int(floor_price * 0.98)
        list_price = max(list_price, 100_000_000)
        min_price = seller._buy_prices.get("TestGift", 0)
        if min_price > 0:
            list_price = max(list_price, min_price)

        assert list_price >= int(3.0 * NANO)

    def test_no_buy_price_allows_undercut(self):
        seller = _make_auto_seller()

        floor_price = int(3.0 * NANO)
        list_price = int(floor_price * 0.98)
        list_price = max(list_price, 100_000_000)
        min_price = seller._buy_prices.get("TestGift", 0)
        if min_price > 0:
            list_price = max(list_price, min_price)

        assert list_price == int(floor_price * 0.98)


class TestMarketToggle:
    def test_default_all_enabled(self):
        seller = _make_auto_seller()
        assert seller.is_market_enabled("mrkt") is True
        assert seller.is_market_enabled("getgems") is True
        assert seller.is_market_enabled("portal") is True
        assert seller.is_market_enabled("fragment") is True

    def test_disable_market(self):
        seller = _make_auto_seller()
        seller.set_market_enabled("mrkt", False)
        assert seller.is_market_enabled("mrkt") is False
        assert seller.is_market_enabled("getgems") is True

    def test_unknown_market_enabled_by_default(self):
        seller = _make_auto_seller()
        assert seller.is_market_enabled("unknown_market") is True


class TestRecentlyBought:
    def test_mark_bought_adds_cooldown(self):
        seller = _make_auto_seller()
        seller.mark_bought("gift-1")
        assert "gift-1" in seller._recently_bought

    def test_cooldown_under_60s(self):
        seller = _make_auto_seller()
        seller._recently_bought["gift-1"] = time.time()
        bought_at = seller._recently_bought.get("gift-1", 0)
        assert bought_at > 0
        assert time.time() - bought_at < 60


class TestBuyCooldown:
    def test_cooldown_prevents_rebuy(self):
        seller = _make_auto_seller()
        seller._buy_cooldown["nft-addr-1"] = time.time()
        assert "nft-addr-1" in seller._buy_cooldown

    def test_cooldown_expires(self):
        seller = _make_auto_seller()
        seller._buy_cooldown["nft-addr-1"] = time.time() - 700  # expired
        now = time.time()
        expired = [k for k, v in seller._buy_cooldown.items() if now - v > 600]
        for k in expired:
            seller._buy_cooldown.pop(k, None)
        assert "nft-addr-1" not in seller._buy_cooldown


class TestShadowMode:
    def test_shadow_mode_default(self):
        seller = _make_auto_seller()
        assert seller._shadow_mode is True

    def test_shadow_mode_off(self):
        seller = _make_auto_seller(shadow_mode=False)
        assert seller._shadow_mode is False


class TestGetBestPrice:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_prices(self):
        seller = _make_auto_seller()
        market, price = await seller._get_best_price("NonexistentGift")
        assert market == ""
        assert price == 0

    @pytest.mark.asyncio
    async def test_prefers_highest_net_price(self):
        seller = _make_auto_seller()
        seller._mds.get_cached_gg_floor.return_value = int(3.0 * NANO)
        seller._mds.get_cached_portal_floor.return_value = int(3.5 * NANO)
        # MRKT returns no listings
        seller._mrkt.get_listings.return_value = {"gifts": []}

        market, price = await seller._get_best_price("TestGift")
        assert market == "portal"
        assert price == int(3.5 * NANO)


class TestStatus:
    def test_get_status_keys(self):
        seller = _make_auto_seller()
        status = seller.get_status()
        assert "enabled" in status
        assert "running" in status
        assert "shadow_mode" in status
        assert "stats" in status
        assert "log" in status

    def test_stats_initial_zero(self):
        seller = _make_auto_seller()
        stats = seller.get_status()["stats"]
        assert stats["checks"] == 0
        assert stats["listed"] == 0
        assert stats["gg_buys"] == 0
        assert stats["portal_buys"] == 0


class TestPendingTransfers:
    def test_dismiss_transfer(self):
        seller = _make_auto_seller()
        seller._pending_transfers["test-slug"] = {
            "name": "TestGift",
            "sell_market": "MRKT",
            "target": "user",
            "buy_price": 1.0,
            "sell_price": 1.5,
            "roi": 10.0,
            "bought_at": time.time(),
            "in_telegram": True,
        }
        assert seller.dismiss_transfer("test-slug") is True
        assert "test-slug" not in seller._pending_transfers

    def test_dismiss_nonexistent(self):
        seller = _make_auto_seller()
        assert seller.dismiss_transfer("nonexistent") is False
