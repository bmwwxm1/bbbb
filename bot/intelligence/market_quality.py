"""Market quality filter — determines if a collection is safe to trade.

Rejects collections with:
  - Low sales velocity (< min threshold per 24h)
  - Thin orderbook (< min listings)
  - Huge spread (> max spread %)
  - Wash-trading patterns (score > threshold)
  - Insufficient depth
"""

from __future__ import annotations

from datetime import timedelta

from bot.data.wash_trade_detector import WashTradeDetector
from bot.models.types import MarketQualityResult, MarketSnapshot

# Thresholds (relaxed for gift market — low velocity is normal)
MIN_SALES_24H = 0  # gifts often have 0 recent sales but still tradeable
MIN_LISTINGS = 1
MAX_SPREAD_PCT = 0.50
MAX_WASH_SCORE = 0.6
MIN_BID_DEPTH = 0


class MarketQualityFilter:
    def __init__(self, wash_detector: WashTradeDetector | None = None) -> None:
        self._wash = wash_detector or WashTradeDetector()

    def evaluate(self, snapshot: MarketSnapshot) -> MarketQualityResult:
        """Check if a collection is tradeable."""
        now = snapshot.timestamp

        # Sales velocity
        sales_24h = [s for s in snapshot.recent_sales if (now - s.sold_at) < timedelta(hours=24)]
        velocity = float(len(sales_24h))

        # Spread
        spread = snapshot.spread_pct

        # Listings
        n_listings = len(snapshot.listings)

        # Bid depth
        bid_depth = len(snapshot.buy_orders)

        # Wash-trade score
        wash_score = self._wash.collection_wash_score(snapshot.recent_sales)

        # Evaluate
        if velocity < MIN_SALES_24H:
            return MarketQualityResult(
                is_tradeable=False,
                reason=f"low_velocity:{velocity:.0f}<{MIN_SALES_24H}",
                sales_velocity_24h=velocity,
                spread_pct=spread,
                listings_count=n_listings,
                bid_depth=bid_depth,
                wash_trade_score=wash_score,
            )

        if n_listings < MIN_LISTINGS:
            return MarketQualityResult(
                is_tradeable=False,
                reason=f"thin_listings:{n_listings}<{MIN_LISTINGS}",
                sales_velocity_24h=velocity,
                spread_pct=spread,
                listings_count=n_listings,
                bid_depth=bid_depth,
                wash_trade_score=wash_score,
            )

        if spread > MAX_SPREAD_PCT:
            return MarketQualityResult(
                is_tradeable=False,
                reason=f"wide_spread:{spread:.0%}>{MAX_SPREAD_PCT:.0%}",
                sales_velocity_24h=velocity,
                spread_pct=spread,
                listings_count=n_listings,
                bid_depth=bid_depth,
                wash_trade_score=wash_score,
            )

        if wash_score > MAX_WASH_SCORE:
            return MarketQualityResult(
                is_tradeable=False,
                reason=f"wash_trading:{wash_score:.2f}>{MAX_WASH_SCORE}",
                sales_velocity_24h=velocity,
                spread_pct=spread,
                listings_count=n_listings,
                bid_depth=bid_depth,
                wash_trade_score=wash_score,
            )

        return MarketQualityResult(
            is_tradeable=True,
            reason="ok",
            sales_velocity_24h=velocity,
            spread_pct=spread,
            listings_count=n_listings,
            bid_depth=bid_depth,
            wash_trade_score=wash_score,
        )
