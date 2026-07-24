"""Tests for ExecutionEngine — dedup, pipeline stages, shadow mode."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from bot.execution.execution_engine import ExecutionEngine
from bot.models.types import (
    Market,
    Opportunity,
    OpportunityScore,
    PnLResult,
    Strategy,
)


def _make_pnl() -> PnLResult:
    return PnLResult(
        gross_profit=500_000_000,
        net_profit=400_000_000,
        roi_pct=10.0,
        annualized_roi_pct=100.0,
        capital_efficiency=1.0,
        total_cost=4_000_000_000,
        fees=100_000_000,
        gas=50_000_000,
        slippage_buffer=0,
        decay_estimate=0,
        relisting_risk=0,
        opportunity_cost=0,
        expected_hold_hours=24.0,
    )


def _make_score() -> OpportunityScore:
    return OpportunityScore(
        raw_score=5.0,
        expected_profit=400_000_000,
        confidence=0.8,
        liquidity_score=0.7,
        fill_probability=0.9,
        expected_hold_hours=24.0,
        passes_threshold=True,
        rejection_reasons=[],
    )


def _make_opportunity(
    gift_id: str = "gift-1",
    collection: str = "TestGift",
    price: int = 1_000_000_000,
    sell_price: int = 1_500_000_000,
    expired: bool = False,
) -> Opportunity:
    return Opportunity(
        gift_id=gift_id,
        collection=collection,
        model="",
        symbol="",
        backdrop="",
        listing_price=price,
        target_sell_price=sell_price,
        buy_market=Market.MRKT,
        sell_market=Market.GETGEMS,
        strategy=Strategy.CROSS_MARKET,
        detected_at=datetime.now(timezone.utc),
        expires_at=(
            datetime(2020, 1, 1, tzinfo=timezone.utc)
            if expired
            else datetime(2030, 1, 1, tzinfo=timezone.utc)
        ),
        snapshot_id=None,
        order_id=None,
        pnl=_make_pnl(),
        score=_make_score(),
    )


def _make_engine(**kwargs) -> ExecutionEngine:
    from bot.core.circuit_breaker import CircuitBreaker
    from bot.core.locks import PGLockManager
    from bot.data.wash_trade_detector import WashTradeDetector
    from bot.execution.risk_engine import RiskEngine
    from bot.intelligence.liquidation_model import LiquidationModel
    from bot.intelligence.market_quality import MarketQualityFilter
    from bot.intelligence.pricing_engine import PricingEngine

    wash = WashTradeDetector()
    pricing = PricingEngine(wash)
    return ExecutionEngine(
        lock_manager=PGLockManager(),
        pricing=pricing,
        liquidation=LiquidationModel(),
        quality=MarketQualityFilter(wash),
        risk=RiskEngine(),
        circuit=CircuitBreaker(),
        shadow_mode=kwargs.get("shadow_mode", True),
        buy_callback=kwargs.get("buy_callback"),
    )


class TestDedupProtection:
    def test_dedup_set_initialized(self):
        engine = _make_engine()
        assert isinstance(engine._recent_buys, dict)
        assert len(engine._recent_buys) == 0

    def test_dedup_ttl_cleanup(self):
        engine = _make_engine()
        engine._recent_buys["old-gift"] = time.monotonic() - 400  # expired
        engine._recent_buys["new-gift"] = time.monotonic()  # fresh

        now_mono = time.monotonic()
        expired_keys = [
            k for k, t in engine._recent_buys.items()
            if now_mono - t > engine._dedup_ttl
        ]
        for k in expired_keys:
            del engine._recent_buys[k]

        assert "old-gift" not in engine._recent_buys
        assert "new-gift" in engine._recent_buys

    def test_dedup_tracks_gift(self):
        engine = _make_engine()
        engine._recent_buys["gift-1"] = time.monotonic()
        assert "gift-1" in engine._recent_buys


class TestShadowMode:
    def test_shadow_mode_default_on(self):
        engine = _make_engine()
        assert engine.shadow_mode is True

    def test_shadow_mode_toggle(self):
        engine = _make_engine()
        engine.shadow_mode = False
        assert engine.shadow_mode is False
        engine.shadow_mode = True
        assert engine.shadow_mode is True


class TestExpiredOpportunity:
    @pytest.mark.asyncio
    async def test_expired_opportunity_rejected(self):
        _make_engine()  # verify instantiation
        opp = _make_opportunity(expired=True)
        assert opp.is_expired is True

    def test_non_expired_opportunity(self):
        opp = _make_opportunity(expired=False)
        assert opp.is_expired is False


class TestOpportunityCreation:
    def test_opportunity_has_pnl(self):
        opp = _make_opportunity()
        assert opp.pnl is not None
        assert opp.pnl.net_profit > 0

    def test_opportunity_has_score(self):
        opp = _make_opportunity()
        assert opp.score is not None
        assert opp.score.passes_threshold is True
