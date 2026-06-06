"""Tests for opportunity scoring — the trade/no-trade decision engine."""

from __future__ import annotations

from bot.intelligence.opportunity_scorer import (
    score_opportunity,
    set_runtime_thresholds,
)
from bot.models.types import (
    NANOTON,
    PnLResult,
    PricingResult,
)


def _make_pnl(
    net_profit: int = int(0.5 * NANOTON),
    roi_pct: float = 15.0,
    hold_hours: float = 4.0,
) -> PnLResult:
    return PnLResult(
        gross_profit=net_profit + int(0.1 * NANOTON),
        net_profit=net_profit,
        roi_pct=roi_pct,
        annualized_roi_pct=roi_pct * 24 / max(hold_hours, 0.5),
        capital_efficiency=roi_pct / 100,
        total_cost=int(3 * NANOTON),
        fees=int(0.05 * NANOTON),
        gas=int(0.05 * NANOTON),
        slippage_buffer=0,
        decay_estimate=0,
        relisting_risk=0,
        opportunity_cost=0,
        expected_hold_hours=hold_hours,
    )


def _make_pricing(
    confidence: float = 0.8,
    liquidity: float = 0.5,
    sell_prob: float = 0.7,
    wash: float = 0.1,
) -> PricingResult:
    return PricingResult(
        fair_value=int(4 * NANOTON),
        confidence=confidence,
        liquidity_score=liquidity,
        sell_probability_24h=sell_prob,
        expected_sell_hours=12.0,
        spread_pct=0.05,
        listings_near_floor=5,
        sales_velocity_24h=3.0,
        rolling_median=int(4 * NANOTON),
        volatility_pct=0.05,
        floor_churn_rate=0.1,
        orderbook_imbalance=0.1,
        wash_trade_score=wash,
    )


class TestScoreOpportunity:
    def test_good_opportunity_passes(self) -> None:
        set_runtime_thresholds(min_roi_pct=2.0)
        score = score_opportunity(_make_pnl(), _make_pricing())
        assert score.passes_threshold is True
        assert len(score.rejection_reasons) == 0
        assert score.raw_score > 0

    def test_low_roi_rejected(self) -> None:
        pnl = _make_pnl(roi_pct=0.5)
        score = score_opportunity(pnl, _make_pricing())
        assert score.passes_threshold is False
        assert any("roi:" in r for r in score.rejection_reasons)

    def test_low_profit_rejected(self) -> None:
        pnl = _make_pnl(net_profit=1000)  # tiny
        score = score_opportunity(pnl, _make_pricing())
        assert score.passes_threshold is False
        assert any("profit:" in r for r in score.rejection_reasons)

    def test_low_confidence_rejected(self) -> None:
        pricing = _make_pricing(confidence=0.01)
        score = score_opportunity(_make_pnl(), pricing)
        assert score.passes_threshold is False
        assert any("confidence:" in r for r in score.rejection_reasons)

    def test_low_liquidity_rejected(self) -> None:
        pricing = _make_pricing(liquidity=0.01)
        score = score_opportunity(_make_pnl(), pricing)
        assert score.passes_threshold is False
        assert any("liquidity:" in r for r in score.rejection_reasons)

    def test_wash_trade_rejected(self) -> None:
        pricing = _make_pricing(wash=0.9)
        score = score_opportunity(_make_pnl(), pricing)
        assert score.passes_threshold is False
        assert any("wash_trade:" in r for r in score.rejection_reasons)

    def test_multiple_rejections_all_listed(self) -> None:
        pnl = _make_pnl(roi_pct=0.1, net_profit=100)
        pricing = _make_pricing(confidence=0.01, liquidity=0.01, wash=0.99)
        score = score_opportunity(pnl, pricing)
        assert score.passes_threshold is False
        assert len(score.rejection_reasons) >= 4

    def test_runtime_thresholds_override(self) -> None:
        """Setting runtime thresholds should affect scoring."""
        set_runtime_thresholds(min_roi_pct=50.0)
        pnl = _make_pnl(roi_pct=20.0)
        score = score_opportunity(pnl, _make_pricing())
        assert any("roi:" in r for r in score.rejection_reasons)
        # Reset
        set_runtime_thresholds(min_roi_pct=2.0)

    def test_hold_hours_floor(self) -> None:
        """Hold hours should be at least 0.5."""
        pnl = _make_pnl(hold_hours=0.0)
        score = score_opportunity(pnl, _make_pricing())
        assert score.expected_hold_hours >= 0.5
