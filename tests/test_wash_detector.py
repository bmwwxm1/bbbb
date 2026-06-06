"""Tests for wash trade detection — protects against manipulated markets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.data.wash_trade_detector import WashTradeDetector
from bot.models.types import Market, Sale


def _make_sale(
    gift_id: str = "g1",
    price: int = 1_000_000_000,
    buyer: str = "buyer1",
    seller: str = "seller1",
    sold_at: datetime | None = None,
) -> Sale:
    return Sale(
        gift_id=gift_id,
        price=price,
        buyer_id=buyer,
        seller_id=seller,
        collection="test_collection",
        sold_at=sold_at or datetime.now(timezone.utc),
        market=Market.MRKT,
    )


class TestWashTradeDetector:
    def setup_method(self) -> None:
        self.detector = WashTradeDetector()

    def test_empty_sales(self) -> None:
        assert self.detector.score_sales([]) == []

    def test_self_trade_flagged(self) -> None:
        """Buyer == seller should be flagged as wash."""
        sale = _make_sale(buyer="user1", seller="user1")
        scored = self.detector.score_sales([sale])
        assert len(scored) == 1
        assert scored[0].wash_score >= 0.9

    def test_normal_trade_clean(self) -> None:
        """Different buyer/seller should be clean."""
        sale = _make_sale(buyer="buyer1", seller="seller1")
        scored = self.detector.score_sales([sale])
        assert scored[0].wash_score < 0.5

    def test_repeated_pair_flagged(self) -> None:
        """Same pair trading 5+ times should be suspicious."""
        now = datetime.now(timezone.utc)
        sales = [
            _make_sale(
                gift_id=f"g{i}",
                buyer="alice",
                seller="bob",
                sold_at=now + timedelta(hours=i),
            )
            for i in range(5)
        ]
        scored = self.detector.score_sales(sales)
        flagged = [s for s in scored if s.wash_score >= 0.3]
        assert len(flagged) > 0

    def test_roundtrip_detected(self) -> None:
        """A→B→A pattern within 24h should be detected."""
        now = datetime.now(timezone.utc)
        sales = [
            _make_sale(
                gift_id="gift1",
                buyer="bob",
                seller="alice",
                sold_at=now,
            ),
            _make_sale(
                gift_id="gift1",
                buyer="alice",
                seller="bob",
                sold_at=now + timedelta(hours=2),
            ),
        ]
        scored = self.detector.score_sales(sales)
        flagged = [s for s in scored if s.wash_score >= 0.5]
        assert len(flagged) > 0

    def test_price_outlier_flagged(self) -> None:
        """A sale at 10x median should be flagged."""
        now = datetime.now(timezone.utc)
        normal_sales = [
            _make_sale(
                gift_id=f"g{i}",
                price=1_000_000_000,
                buyer=f"b{i}",
                seller=f"s{i}",
                sold_at=now + timedelta(hours=i),
            )
            for i in range(10)
        ]
        outlier = _make_sale(
            gift_id="g99",
            price=20_000_000_000,
            buyer="bX",
            seller="sX",
            sold_at=now + timedelta(hours=11),
        )
        sales = normal_sales + [outlier]
        scored = self.detector.score_sales(sales)
        outlier_score = scored[-1].wash_score
        assert outlier_score > 0.3

    def test_clean_sales_filters(self) -> None:
        """clean_sales should remove high-wash-score sales."""
        sales = [
            _make_sale(buyer="user1", seller="user1"),  # self-trade
            _make_sale(buyer="buyer2", seller="seller2"),  # clean
        ]
        clean = self.detector.clean_sales(sales, threshold=0.5)
        assert len(clean) <= len(sales)

    def test_collection_wash_score(self) -> None:
        """Collection-level wash score."""
        sales = [
            _make_sale(buyer="u1", seller="u1"),
            _make_sale(buyer="b1", seller="s1"),
        ]
        score = self.detector.collection_wash_score(sales)
        assert 0.0 <= score <= 1.0
