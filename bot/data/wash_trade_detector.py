"""Wash trade detection and outlier rejection for sales data.

Scoring signals (each 0.0–1.0, combined into wash_score):
  1. Self-trade: buyer == seller
  2. Repeated pairs: same buyer/seller pair appears > threshold times
  3. Velocity anomaly: burst of sales far above normal rate
  4. Price outlier: sale price far from rolling median
  5. Round-trip: A→B→A pattern within short time window
"""

from __future__ import annotations

import bisect
import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from bot.models.types import Sale, ScoredSale


class WashTradeDetector:
    """Stateless detector — call score_sales() with a batch of recent sales."""

    def __init__(
        self,
        *,
        pair_repeat_threshold: int = 2,
        velocity_window_minutes: int = 30,
        velocity_spike_factor: float = 3.0,
        price_outlier_sigma: float = 2.5,
        roundtrip_window_hours: float = 24.0,
    ) -> None:
        self._pair_threshold = pair_repeat_threshold
        self._velocity_window = timedelta(minutes=velocity_window_minutes)
        self._velocity_spike = velocity_spike_factor
        self._outlier_sigma = price_outlier_sigma
        self._roundtrip_window = timedelta(hours=roundtrip_window_hours)

    def score_sales(self, sales: list[Sale]) -> list[ScoredSale]:
        """Score each sale for wash-trade probability."""
        if not sales:
            return []

        pair_scores = self._score_repeated_pairs(sales)
        velocity_scores = self._score_velocity_anomalies(sales)
        price_scores = self._score_price_outliers(sales)
        roundtrip_ids = self._detect_roundtrips(sales)

        result: list[ScoredSale] = []
        for i, sale in enumerate(sales):
            signals: list[float] = []

            # 1. Self-trade
            if sale.buyer_id and sale.seller_id and sale.buyer_id == sale.seller_id:
                signals.append(1.0)
            else:
                signals.append(0.0)

            # 2. Repeated pairs
            signals.append(pair_scores.get(i, 0.0))

            # 3. Velocity anomaly
            signals.append(velocity_scores.get(i, 0.0))

            # 4. Price outlier
            signals.append(price_scores.get(i, 0.0))

            # 5. Round-trip
            signals.append(1.0 if sale.gift_id in roundtrip_ids else 0.0)

            # Composite: max of all signals (conservative — one red flag is enough)
            wash_score = max(signals) if signals else 0.0

            result.append(ScoredSale(sale=sale, wash_score=wash_score))

        return result

    def clean_sales(self, sales: list[Sale], threshold: float = 0.5) -> list[Sale]:
        """Return only sales with wash_score below threshold."""
        scored = self.score_sales(sales)
        return [s.sale for s in scored if s.wash_score < threshold]

    def collection_wash_score(self, sales: list[Sale]) -> float:
        """Overall wash-trade score for a collection (0.0–1.0)."""
        if not sales:
            return 0.0
        scored = self.score_sales(sales)
        dirty = sum(1 for s in scored if s.wash_score >= 0.5)
        return dirty / len(scored)

    # ── Signal Scorers ─────────────────────────────────────────────────

    def _score_repeated_pairs(self, sales: list[Sale]) -> dict[int, float]:
        """Sales between the same buyer/seller pair repeated too often."""
        pair_count: Counter[tuple[str, str]] = Counter()
        pair_indices: defaultdict[tuple[str, str], list[int]] = defaultdict(list)

        for i, s in enumerate(sales):
            if s.buyer_id and s.seller_id:
                pair = (min(s.buyer_id, s.seller_id), max(s.buyer_id, s.seller_id))
                pair_count[pair] += 1
                pair_indices[pair].append(i)

        scores: dict[int, float] = {}
        for pair, count in pair_count.items():
            if count > self._pair_threshold:
                score = min(count / (self._pair_threshold * 3), 1.0)
                for idx in pair_indices[pair]:
                    scores[idx] = score

        return scores

    def _score_velocity_anomalies(self, sales: list[Sale]) -> dict[int, float]:
        """Burst of sales far above normal rate (O(n log n) via bisect)."""
        if len(sales) < 5:
            return {}

        sorted_sales = sorted(enumerate(sales), key=lambda x: x[1].sold_at)
        timestamps = [s.sold_at.timestamp() for _, s in sorted_sales]
        scores: dict[int, float] = {}

        total_span = timestamps[-1] - timestamps[0]
        if total_span <= 0:
            return {}

        window_sec = self._velocity_window.total_seconds()
        expected_rate = len(sales) / (total_span / window_sec)
        if expected_rate <= 0:
            return {}

        for i, (orig_idx, _sale) in enumerate(sorted_sales):
            window_start = timestamps[i] - window_sec
            left = bisect.bisect_left(timestamps, window_start)
            count_in_window = i - left + 1

            if count_in_window > expected_rate * self._velocity_spike:
                score = min(
                    (count_in_window - expected_rate) / (expected_rate * self._velocity_spike),
                    1.0,
                )
                scores[orig_idx] = score

        return scores

    def _score_price_outliers(self, sales: list[Sale]) -> dict[int, float]:
        """Sales at prices far from median."""
        prices = [s.price for s in sales if s.price > 0]
        if len(prices) < 3:
            return {}

        med = statistics.median(prices)
        try:
            std = statistics.stdev(prices)
        except statistics.StatisticsError:
            return {}

        if std <= 0:
            return {}

        scores: dict[int, float] = {}
        for i, sale in enumerate(sales):
            if sale.price <= 0:
                continue
            z_score = abs(sale.price - med) / std
            if z_score > self._outlier_sigma:
                scores[i] = min(z_score / (self._outlier_sigma * 2), 1.0)

        return scores

    def _detect_roundtrips(self, sales: list[Sale]) -> set[str]:
        """Detect A→B→A patterns: same gift traded back within time window."""
        gift_trades: defaultdict[str, list[Sale]] = defaultdict(list)
        for s in sales:
            if s.gift_id:
                gift_trades[s.gift_id].append(s)

        roundtrip_gifts: set[str] = set()
        for gift_id, trades in gift_trades.items():
            if len(trades) < 2:
                continue

            trades.sort(key=lambda s: s.sold_at)
            for i in range(len(trades) - 1):
                a = trades[i]
                b = trades[i + 1]
                # Same gift goes back to original seller
                if a.seller_id and b.buyer_id == a.seller_id:
                    if (b.sold_at - a.sold_at) < self._roundtrip_window:
                        roundtrip_gifts.add(gift_id)
                        break

        return roundtrip_gifts
