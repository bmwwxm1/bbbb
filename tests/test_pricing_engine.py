"""Tests for pricing engine — fair value estimation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.intelligence.pricing_engine import PricingEngine
from bot.models.types import (
    NANOTON,
    Listing,
    Market,
    MarketSnapshot,
    Order,
    PricingResult,
    Sale,
)


def _ton(n: float) -> int:
    return int(n * NANOTON)


def _make_snapshot(
    floor: float = 5.0,
    n_listings: int = 10,
    n_sales: int = 8,
    n_orders: int = 3,
) -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    listings = [
        Listing(
            gift_id=f"l{i}",
            collection="test",
            model="model1",
            symbol="symbol1",
            backdrop="backdrop1",
            price=_ton(floor + i * 0.1),
            seller_id=f"seller{i}",
            listed_at=now - timedelta(hours=i),
            market=Market.MRKT,
        )
        for i in range(n_listings)
    ]
    sales = [
        Sale(
            gift_id=f"s{i}",
            price=_ton(floor + (i % 3) * 0.2 - 0.1),
            buyer_id=f"buyer{i}",
            seller_id=f"seller{i + 10}",
            collection="test",
            sold_at=now - timedelta(hours=i),
            market=Market.MRKT,
        )
        for i in range(n_sales)
    ]
    orders = [
        Order(
            order_id=f"o{i}",
            collection="test",
            model=None,
            symbol=None,
            backdrop=None,
            price_min=_ton(floor - 1),
            price_max=_ton(floor - 0.5 - i * 0.1),
            quantity_total=5,
            quantity_filled=1,
            creator_id=f"orderer{i}",
            created_at=now - timedelta(hours=i),
        )
        for i in range(n_orders)
    ]
    return MarketSnapshot(
        collection="test",
        market=Market.MRKT,
        timestamp=now,
        floor_price=listings[0].price if listings else 0,
        listings=listings,
        buy_orders=orders,
        recent_sales=sales,
        data_age_seconds=0.0,
    )


class TestPricingEngine:
    def setup_method(self) -> None:
        self.engine = PricingEngine()

    def test_basic_pricing(self) -> None:
        snap = _make_snapshot()
        result = self.engine.price(snap)
        assert isinstance(result, PricingResult)
        assert result.fair_value > 0
        assert 0.0 <= result.confidence <= 1.0
        assert 0.0 <= result.liquidity_score <= 1.0
        assert 0.0 <= result.wash_trade_score <= 1.0

    def test_empty_listings(self) -> None:
        snap = _make_snapshot(n_listings=0, n_sales=0, n_orders=0)
        result = self.engine.price(snap)
        assert result.confidence < 0.5

    def test_sell_probability_bounded(self) -> None:
        snap = _make_snapshot()
        result = self.engine.price(snap)
        assert 0.0 <= result.sell_probability_24h <= 1.0

    def test_high_liquidity_scores_better(self) -> None:
        low_liq = _make_snapshot(n_listings=2, n_sales=1, n_orders=0)
        high_liq = _make_snapshot(n_listings=20, n_sales=15, n_orders=5)
        r_low = self.engine.price(low_liq)
        r_high = self.engine.price(high_liq)
        assert r_high.liquidity_score >= r_low.liquidity_score

    def test_previous_snapshot_cached(self) -> None:
        snap1 = _make_snapshot()
        self.engine.price(snap1)
        assert "test" in self.engine._prev_snapshots
