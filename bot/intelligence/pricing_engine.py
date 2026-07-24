"""Fair value estimation and 8-factor confidence model.

Uses CLEANED sales only (wash trades removed by WashTradeDetector).

Fair value = weighted composite of:
  - Rolling median of recent sales (40%)
  - Time-weighted average of sales (30%)
  - Orderbook mid-price (20%)
  - Listing density adjustment (10%)

Confidence = weighted average of 8 factors (each 0.0–1.0):
  1. liquidity_depth       (0.20) — listings near floor + bid depth
  2. spread_stability      (0.15) — low variance in spread
  3. volatility            (0.15) — inverse: low vol = high confidence
  4. sales_consistency     (0.15) — regular pattern vs sporadic bursts
  5. orderbook_imbalance   (0.10) — balanced = higher confidence
  6. last_sale_recency     (0.10) — hours since last sale
  7. floor_churn_rate      (0.10) — stable floor = better
  8. inventory_turnover    (0.05) — % of listings that sell in 24h
"""

from __future__ import annotations

import statistics
from datetime import datetime

from bot.data.wash_trade_detector import WashTradeDetector
from bot.models.types import MarketSnapshot, PricingResult


class PricingEngine:
    def __init__(self, wash_detector: WashTradeDetector | None = None) -> None:
        self._wash = wash_detector or WashTradeDetector()
        # Cache previous snapshots for churn/stability calculations
        self._prev_snapshots: dict[str, MarketSnapshot] = {}

    def price(self, snapshot: MarketSnapshot) -> PricingResult:
        """Compute fair value and confidence for a collection snapshot."""
        now = snapshot.timestamp

        # Clean sales
        clean_sales = self._wash.clean_sales(snapshot.recent_sales)
        wash_score = self._wash.collection_wash_score(snapshot.recent_sales)

        prices = [s.price for s in clean_sales if s.price > 0]

        # ── Fair value components ──────────────────────────────────────

        rolling_median = int(statistics.median(prices)) if len(prices) >= 3 else None

        weighted_avg = self._time_weighted_avg(clean_sales, now)

        best_bid = snapshot.best_bid
        best_ask = snapshot.floor_price
        mid_price = (best_bid + best_ask) // 2 if best_bid > 0 else best_ask

        density_adj = self._listing_density_adjustment(snapshot)

        # Weighted composite
        components: list[tuple[int | None, float]] = [
            (rolling_median, 0.40),
            (weighted_avg, 0.30),
            (mid_price if mid_price > 0 else None, 0.20),
            (density_adj if density_adj > 0 else None, 0.10),
        ]

        total_weight = 0.0
        weighted_sum = 0.0
        for value, weight in components:
            if value is not None and value > 0:
                weighted_sum += value * weight
                total_weight += weight

        if total_weight > 0:
            fair_value = int(weighted_sum / total_weight)
        else:
            fair_value = snapshot.floor_price

        # ── Confidence model (8 factors) ───────────────────────────────

        factors = self._compute_confidence_factors(snapshot, clean_sales, now)
        confidence = self._weighted_confidence(factors)

        # ── Derived metrics ────────────────────────────────────────────

        spread_pct = snapshot.spread_pct
        listings_near_floor = self._count_near_floor(snapshot, pct=0.05)

        sales_24h = [s for s in clean_sales if (now - s.sold_at).total_seconds() < 86400]
        velocity_24h = float(len(sales_24h))

        volatility_pct = 0.0
        if len(prices) >= 3:
            mean_p = statistics.mean(prices)
            if mean_p > 0:
                volatility_pct = statistics.stdev(prices) / mean_p

        floor_churn = factors.get("floor_churn_rate", 0.0)
        ob_imbalance = factors.get("orderbook_imbalance", 0.0)

        sell_prob_24h = self._estimate_sell_probability(
            velocity_24h, listings_near_floor, confidence
        )
        expected_hours = 24.0 / max(sell_prob_24h, 0.01)

        # Cache for next comparison
        self._prev_snapshots[snapshot.collection] = snapshot

        return PricingResult(
            fair_value=fair_value,
            confidence=confidence,
            liquidity_score=factors.get("liquidity_depth", 0.0),
            sell_probability_24h=sell_prob_24h,
            expected_sell_hours=min(expected_hours, 168.0),
            spread_pct=spread_pct,
            listings_near_floor=listings_near_floor,
            sales_velocity_24h=velocity_24h,
            rolling_median=rolling_median,
            volatility_pct=volatility_pct,
            floor_churn_rate=floor_churn,
            orderbook_imbalance=ob_imbalance,
            wash_trade_score=wash_score,
            confidence_factors=factors,
        )

    # ── Fair Value Helpers ─────────────────────────────────────────────

    def _time_weighted_avg(self, sales: list, now: datetime) -> int | None:
        """More recent sales have higher weight: weight = 1 / age_hours."""
        if not sales:
            return None

        total_weight = 0.0
        weighted_sum = 0.0
        for s in sales:
            age_hours = max((now - s.sold_at).total_seconds() / 3600, 0.5)
            weight = 1.0 / age_hours
            weighted_sum += s.price * weight
            total_weight += weight

        if total_weight <= 0:
            return None
        return int(weighted_sum / total_weight)

    def _listing_density_adjustment(self, snapshot: MarketSnapshot) -> int:
        """If many listings cluster near floor, fair value is near floor."""
        if not snapshot.listings:
            return snapshot.floor_price

        near_floor = self._count_near_floor(snapshot, pct=0.05)
        total = len(snapshot.listings)

        if total <= 0:
            return snapshot.floor_price

        density_ratio = near_floor / total
        # High density near floor → pull fair value toward floor
        if density_ratio > 0.5:
            return int(snapshot.floor_price * 1.02)
        return int(snapshot.floor_price * 1.05)

    def _count_near_floor(self, snapshot: MarketSnapshot, pct: float) -> int:
        if snapshot.floor_price <= 0:
            return 0
        threshold = int(snapshot.floor_price * (1 + pct))
        return sum(1 for item in snapshot.listings if item.price <= threshold)

    # ── 8-Factor Confidence ────────────────────────────────────────────

    def _compute_confidence_factors(
        self,
        snapshot: MarketSnapshot,
        clean_sales: list,
        now: datetime,
    ) -> dict[str, float]:
        factors: dict[str, float] = {}

        # 1. Liquidity depth (listings + bids)
        n_listings = len(snapshot.listings)
        n_bids = len(snapshot.buy_orders)
        near_floor = self._count_near_floor(snapshot, pct=0.05)
        liquidity_raw = min(near_floor / 5, 1.0) * 0.6 + min(n_bids / 3, 1.0) * 0.4
        factors["liquidity_depth"] = min(liquidity_raw, 1.0)

        # 2. Spread stability (use current spread as proxy)
        spread = snapshot.spread_pct
        factors["spread_stability"] = max(1.0 - spread * 2, 0.0)

        # 3. Volatility (inverse)
        prices = [s.price for s in clean_sales if s.price > 0]
        if len(prices) >= 3:
            mean_p = statistics.mean(prices)
            if mean_p > 0:
                cv = statistics.stdev(prices) / mean_p
                factors["volatility"] = max(1.0 - cv * 3, 0.0)
            else:
                factors["volatility"] = 0.0
        else:
            factors["volatility"] = 0.0

        # 4. Sales consistency (regular vs sporadic)
        factors["sales_consistency"] = self._sales_consistency(clean_sales, now)

        # 5. Orderbook imbalance
        bid_vol = sum(
            (o.quantity_total - o.quantity_filled) * o.price_max for o in snapshot.buy_orders
        )
        ask_vol = sum(item.price for item in snapshot.listings[:20])
        total_vol = bid_vol + ask_vol
        if total_vol > 0:
            imbalance = abs(bid_vol - ask_vol) / total_vol
            factors["orderbook_imbalance"] = max(1.0 - imbalance, 0.0)
        else:
            factors["orderbook_imbalance"] = 0.0

        # 6. Last sale recency
        if clean_sales:
            most_recent = max(s.sold_at for s in clean_sales)
            hours_ago = (now - most_recent).total_seconds() / 3600
            factors["last_sale_recency"] = max(1.0 - hours_ago / 48, 0.0)
        else:
            factors["last_sale_recency"] = 0.0

        # 7. Floor churn rate
        prev = self._prev_snapshots.get(snapshot.collection)
        if prev and prev.floor_price > 0 and snapshot.floor_price > 0:
            change = abs(snapshot.floor_price - prev.floor_price) / prev.floor_price
            factors["floor_churn_rate"] = max(1.0 - change * 5, 0.0)
        else:
            factors["floor_churn_rate"] = 0.5  # neutral when no history

        # 8. Inventory turnover (proxy: sales/listings ratio)
        sales_24h = [s for s in clean_sales if (now - s.sold_at).total_seconds() < 86400]
        if n_listings > 0:
            turnover = len(sales_24h) / n_listings
            factors["inventory_turnover"] = min(turnover, 1.0)
        else:
            factors["inventory_turnover"] = 0.0

        return factors

    @staticmethod
    def _weighted_confidence(factors: dict[str, float]) -> float:
        weights = {
            "liquidity_depth": 0.20,
            "spread_stability": 0.15,
            "volatility": 0.15,
            "sales_consistency": 0.15,
            "orderbook_imbalance": 0.10,
            "last_sale_recency": 0.10,
            "floor_churn_rate": 0.10,
            "inventory_turnover": 0.05,
        }
        total = 0.0
        for name, weight in weights.items():
            total += factors.get(name, 0.0) * weight
        return min(max(total, 0.0), 1.0)

    @staticmethod
    def _sales_consistency(sales: list, now: datetime) -> float:
        """Regular sales pattern = high score, sporadic bursts = low."""
        if len(sales) < 3:
            return 0.0

        sorted_sales = sorted(sales, key=lambda s: s.sold_at)
        gaps: list[float] = []
        for i in range(1, len(sorted_sales)):
            gap = (sorted_sales[i].sold_at - sorted_sales[i - 1].sold_at).total_seconds()
            gaps.append(gap)

        if not gaps:
            return 0.0

        mean_gap = statistics.mean(gaps)
        if mean_gap <= 0:
            return 0.0

        try:
            cv = statistics.stdev(gaps) / mean_gap
        except statistics.StatisticsError:
            return 0.0

        # Low CV = consistent, high CV = sporadic
        return max(1.0 - cv / 3, 0.0)

    @staticmethod
    def _estimate_sell_probability(
        velocity_24h: float,
        listings_near_floor: int,
        confidence: float,
    ) -> float:
        """Rough estimate of selling within 24h at floor-competitive price."""
        if velocity_24h <= 0:
            return 0.01

        # If we list near floor with N competitors and V sales/day
        if listings_near_floor <= 0:
            listings_near_floor = 1

        base_prob = min(velocity_24h / (listings_near_floor + 1), 1.0)
        return base_prob * max(confidence, 0.1)
