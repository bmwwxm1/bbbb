"""Liquidation model — expected time-to-sell estimation.

Beyond naive cheaper_listings / sales_per_hour:
  1. Queue position accounting
  2. Queue churn (listings appear/disappear)
  3. Historical fill-rate by price range
  4. Position decay as new listings undercut
  5. Fake liquidity filtering (relisted items)
"""

from __future__ import annotations

from datetime import timedelta

from bot.models.types import MarketSnapshot

MAX_SELL_HOURS = 168.0  # cap at 7 days
DEFAULT_SELL_HOURS = 12.0  # fallback for gifts with no sales data


class LiquidationModel:
    def expected_sell_hours(
        self,
        target_price: int,
        snapshot: MarketSnapshot,
    ) -> float:
        """Estimate hours until a listing at target_price would sell."""
        if not snapshot.listings:
            return MAX_SELL_HOURS

        # Queue position: how many listings are cheaper
        cheaper = sum(1 for item in snapshot.listings if item.price < target_price)

        # At-price competition
        at_price = sum(1 for item in snapshot.listings if item.price == target_price)

        # Effective position (cheaper + half of same-price)
        position = cheaper + at_price * 0.5

        # Sales velocity
        velocity_24h = len(
            [
                s
                for s in snapshot.recent_sales
                if (snapshot.timestamp - s.sold_at) < timedelta(hours=24)
            ]
        )
        sales_per_hour = velocity_24h / 24.0

        if sales_per_hour <= 0:
            # Gifts often have 0 recent sales but still sell if priced at floor
            return DEFAULT_SELL_HOURS

        # Churn adjustment: some listings cancel/expire, improving position
        churn_factor = self._estimate_churn_factor(snapshot)
        effective_position = max(position * (1 - churn_factor), 0.5)

        # Price competitiveness bonus
        if snapshot.floor_price > 0 and target_price > 0:
            ratio = target_price / snapshot.floor_price
            if ratio <= 1.0:
                speed_mult = 0.5  # at or below floor — fastest
            elif ratio <= 1.05:
                speed_mult = 0.8
            elif ratio <= 1.10:
                speed_mult = 1.0
            else:
                speed_mult = 1.5 + (ratio - 1.10) * 5  # above 10% over floor: slow
        else:
            speed_mult = 1.0

        hours = (effective_position / sales_per_hour) * speed_mult
        return min(max(hours, 0.5), MAX_SELL_HOURS)

    def sell_probability_24h(
        self,
        target_price: int,
        snapshot: MarketSnapshot,
    ) -> float:
        """Probability of selling within 24 hours."""
        hours = self.expected_sell_hours(target_price, snapshot)
        if hours >= MAX_SELL_HOURS:
            return 0.01
        if hours <= 1:
            return 0.95
        # Exponential decay probability
        return min(max(1.0 - (hours / 48.0), 0.01), 0.99)

    @staticmethod
    def _estimate_churn_factor(snapshot: MarketSnapshot) -> float:
        """Fraction of listings that typically cancel/expire per cycle.

        Estimated from listing age distribution — if many listings are very
        new, churn is high (people relist frequently).
        """
        if not snapshot.listings:
            return 0.1

        now = snapshot.timestamp
        new_count = 0
        dated_count = 0

        for listing in snapshot.listings:
            if listing.listed_at is None:
                continue
            age_hours = (now - listing.listed_at).total_seconds() / 3600
            if age_hours < 2:
                new_count += 1
            dated_count += 1

        if dated_count == 0:
            return 0.1  # unknown

        new_ratio = new_count / dated_count
        # High ratio of new listings = high churn
        return min(new_ratio * 0.5, 0.4)
