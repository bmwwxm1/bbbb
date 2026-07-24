"""Tests for PnL calculation — the core trading math."""

from __future__ import annotations

from bot.intelligence.cost_model import (
    MRKT_SELL_FEE_PCT,
    annualized_roi,
    calculate_pnl,
    opportunity_cost,
)
from bot.models.types import NANOTON


def _ton(n: float) -> int:
    return int(n * NANOTON)


class TestCalculatePnl:
    def test_basic_cross_market(self) -> None:
        """Buy on MRKT at 10 TON, sell on Getgems at 12 TON."""
        pnl = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(12),
            sell_market="getgems",
            needs_transfer=True,
        )
        assert pnl.gross_profit > 0
        assert pnl.net_profit > 0
        assert pnl.roi_pct > 0
        assert pnl.total_cost > _ton(10)

    def test_negative_profit_detected(self) -> None:
        """Buy at 10 TON, sell at 10.1 TON — fees eat the profit."""
        pnl = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(10.1),
            sell_market="getgems",
            needs_transfer=True,
        )
        assert pnl.net_profit < 0

    def test_zero_buy_price(self) -> None:
        """Zero buy price should return zero PnL (guard clause)."""
        pnl = calculate_pnl(buy_price=0, expected_sell_price=_ton(10))
        assert pnl.net_profit == 0
        assert pnl.roi_pct == 0.0

    def test_zero_sell_price(self) -> None:
        """Zero sell price should return zero PnL."""
        pnl = calculate_pnl(buy_price=_ton(10), expected_sell_price=0)
        assert pnl.net_profit == 0

    def test_fragment_withdraw_free(self) -> None:
        """Fragment withdraw fee is 0 (same account)."""
        pnl_frag = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(12),
            sell_market="fragment",
            needs_transfer=True,
        )
        pnl_gg = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(12),
            sell_market="getgems",
            needs_transfer=True,
        )
        # Fragment should be cheaper (no withdraw fee)
        assert pnl_frag.net_profit > pnl_gg.net_profit

    def test_sell_market_case_insensitive(self) -> None:
        """Sell market matching should work regardless of case."""
        for name in ("mrkt", "MRKT", "Mrkt"):
            pnl = calculate_pnl(
                buy_price=_ton(10),
                expected_sell_price=_ton(15),
                sell_market=name,
                sell_market_fee_pct=MRKT_SELL_FEE_PCT,
            )
            assert pnl.net_profit != 0

    def test_no_transfer_costs(self) -> None:
        """Without transfer, costs should be lower."""
        pnl_transfer = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(15),
            needs_transfer=True,
        )
        pnl_no_transfer = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(15),
            needs_transfer=False,
        )
        assert pnl_no_transfer.net_profit > pnl_transfer.net_profit

    def test_longer_hold_more_decay(self) -> None:
        """Longer hold = more decay = less profit."""
        pnl_short = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(15),
            expected_hold_hours=1.0,
        )
        pnl_long = calculate_pnl(
            buy_price=_ton(10),
            expected_sell_price=_ton(15),
            expected_hold_hours=168.0,
        )
        assert pnl_short.net_profit > pnl_long.net_profit

    def test_fees_never_negative(self) -> None:
        """Fees should never be negative."""
        pnl = calculate_pnl(
            buy_price=_ton(5),
            expected_sell_price=_ton(6),
        )
        assert pnl.fees >= 0
        assert pnl.gas >= 0


class TestAnnualizedRoi:
    def test_capped(self) -> None:
        """Annualized ROI should be capped to prevent overflow."""
        result = annualized_roi(100.0, 0.001)
        assert result <= 100_000.0

    def test_zero_hours(self) -> None:
        result = annualized_roi(10.0, 0.0)
        assert result <= 100_000.0

    def test_one_year_hold(self) -> None:
        """If holding for exactly 1 year, ann ROI = ROI."""
        result = annualized_roi(10.0, 8760.0)
        assert abs(result - 10.0) < 0.01

    def test_negative_roi(self) -> None:
        """Negative ROI should still work."""
        result = annualized_roi(-5.0, 24.0)
        assert result < 0


class TestOpportunityCost:
    def test_zero_hold(self) -> None:
        assert opportunity_cost(_ton(100), 0.0) == 0

    def test_positive_hold(self) -> None:
        cost = opportunity_cost(_ton(100), 24.0)
        assert cost > 0

    def test_longer_hold_more_cost(self) -> None:
        cost_short = opportunity_cost(_ton(100), 1.0)
        cost_long = opportunity_cost(_ton(100), 100.0)
        assert cost_long > cost_short
