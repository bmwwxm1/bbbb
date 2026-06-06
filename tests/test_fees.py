"""Tests for fee calculations — ensures no negative-profit trades can pass."""

from __future__ import annotations

from bot.fees import (
    cross_market_profit,
    cross_market_roi,
    net_sell_price,
    net_sell_price_getgems,
    net_sell_price_mrkt,
    net_sell_price_portal,
    total_buy_cost,
    total_buy_cost_mrkt,
    within_mrkt_profit,
    within_mrkt_roi,
)

NANO = 1_000_000_000


def _ton(n: float) -> int:
    return int(n * NANO)


class TestNetSellPrice:
    def test_mrkt_sell_fee_applied(self) -> None:
        net = net_sell_price_mrkt(_ton(10))
        assert net < _ton(10)
        assert net > 0

    def test_getgems_sell_no_fee(self) -> None:
        """Getgems sell fee for gifts is currently 0%."""
        net = net_sell_price_getgems(_ton(10))
        # If fee is 0, net == gross
        assert net <= _ton(10)
        assert net > 0

    def test_fragment_no_fee(self) -> None:
        net = net_sell_price(_ton(10), "fragment")
        assert net == _ton(10)

    def test_portal_sell_zero_fee(self) -> None:
        """Portal has 0% sell fee."""
        net = net_sell_price_portal(_ton(10))
        assert net == _ton(10)

    def test_portal_via_generic(self) -> None:
        net = net_sell_price(_ton(10), "portal")
        assert net == _ton(10)


class TestTotalBuyCost:
    def test_buy_cost_includes_fee(self) -> None:
        cost = total_buy_cost_mrkt(_ton(10))
        assert cost >= _ton(10)

    def test_portal_buy_zero_fee(self) -> None:
        """Portal has 0% buy fee."""
        cost = total_buy_cost(_ton(10), "portal")
        assert cost == _ton(10)

    def test_generic_buy_cost(self) -> None:
        """Generic function routes to correct market."""
        assert total_buy_cost(_ton(10), "mrkt") == total_buy_cost_mrkt(_ton(10))
        assert total_buy_cost(_ton(10), "getgems") == _ton(10)


class TestCrossMarketProfit:
    def test_profitable_trade(self) -> None:
        """Buy MRKT 5 TON, sell Getgems 7 TON — should be profitable."""
        profit = cross_market_profit(_ton(5), _ton(7))
        assert profit > 0

    def test_unprofitable_trade(self) -> None:
        """Buy 10 TON, sell 10 TON — fees eat profit."""
        profit = cross_market_profit(_ton(10), _ton(10))
        assert profit < 0

    def test_zero_prices(self) -> None:
        roi = cross_market_roi(0, _ton(10))
        assert roi == 0.0

    def test_portal_buy_sell_mrkt(self) -> None:
        """Buy on Portal (0% fee) → sell on MRKT (5% fee)."""
        profit = cross_market_profit(
            _ton(5), _ton(7), buy_market="portal", sell_market="mrkt"
        )
        assert profit > 0

    def test_mrkt_buy_sell_portal(self) -> None:
        """Buy on MRKT → sell on Portal (0% fee)."""
        profit = cross_market_profit(
            _ton(5), _ton(7), buy_market="mrkt", sell_market="portal"
        )
        assert profit > 0

    def test_portal_to_portal_no_arb(self) -> None:
        """Same price same market = loss from withdrawal."""
        profit = cross_market_profit(
            _ton(10), _ton(10), buy_market="portal", sell_market="portal"
        )
        assert profit < 0  # withdrawal fee


class TestWithinMrktProfit:
    def test_profitable_within_mrkt(self) -> None:
        """Buy listing 5 TON, sell into order 6 TON."""
        profit = within_mrkt_profit(_ton(5), _ton(6))
        assert profit > 0

    def test_roi_calculation(self) -> None:
        roi = within_mrkt_roi(_ton(5), _ton(6))
        assert roi > 0
        assert roi < 100  # sanity check

    def test_zero_buy_price(self) -> None:
        roi = within_mrkt_roi(0, _ton(10))
        assert roi == 0.0
