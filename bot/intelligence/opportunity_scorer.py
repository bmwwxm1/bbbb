"""Opportunity scoring — decides whether to execute a trade.

OpportunityScore = profit x confidence x liquidity x fill_probability / hold_time

Only trade top percentile opportunities.
Every rejection reason is explicit and logged.
"""

from __future__ import annotations

import threading

from bot.models.types import (
    MarketQualityResult,
    OpportunityScore,
    PnLResult,
    PricingResult,
    nanoton_to_ton,
    ton_to_nanoton,
)

# Default minimum thresholds (can be overridden via runtime settings)
MIN_ROI_PCT = 2.0
MIN_ABSOLUTE_PROFIT = ton_to_nanoton(0.05)
MIN_CONFIDENCE = 0.1
MIN_LIQUIDITY = 0.05
MAX_WASH_SCORE = 0.7
SCORE_THRESHOLD = 0.0

# Thread-safe runtime overrides
_lock = threading.Lock()
_runtime_min_roi: float | None = None
_runtime_min_profit: int | None = None


def set_runtime_thresholds(
    min_roi_pct: float | None = None, min_profit_ton: float | None = None
) -> None:
    global _runtime_min_roi, _runtime_min_profit
    with _lock:
        if min_roi_pct is not None:
            _runtime_min_roi = min_roi_pct
        if min_profit_ton is not None:
            _runtime_min_profit = ton_to_nanoton(min_profit_ton)


def _get_thresholds() -> tuple[float, int]:
    with _lock:
        min_roi = _runtime_min_roi if _runtime_min_roi is not None else MIN_ROI_PCT
        min_profit = _runtime_min_profit if _runtime_min_profit is not None else MIN_ABSOLUTE_PROFIT
    return min_roi, min_profit


def score_opportunity(
    pnl: PnLResult,
    pricing: PricingResult,
    quality: MarketQualityResult | None = None,
) -> OpportunityScore:
    """Score a potential trade. Collects ALL rejection reasons."""

    rejection_reasons: list[str] = []

    min_roi, min_profit = _get_thresholds()

    if pnl.roi_pct < min_roi:
        rejection_reasons.append(f"roi:{pnl.roi_pct:.1f}%<{min_roi}%")

    if pnl.net_profit < min_profit:
        profit_ton = nanoton_to_ton(pnl.net_profit)
        min_ton = nanoton_to_ton(min_profit)
        rejection_reasons.append(f"profit:{profit_ton:.2f}<{min_ton:.2f}TON")

    if pricing.confidence < MIN_CONFIDENCE:
        rejection_reasons.append(f"confidence:{pricing.confidence:.2f}<{MIN_CONFIDENCE}")

    if pricing.liquidity_score < MIN_LIQUIDITY:
        rejection_reasons.append(f"liquidity:{pricing.liquidity_score:.2f}<{MIN_LIQUIDITY}")

    if pricing.wash_trade_score > MAX_WASH_SCORE:
        rejection_reasons.append(f"wash_trade:{pricing.wash_trade_score:.2f}>{MAX_WASH_SCORE}")

    # Market quality check
    if quality and not quality.is_tradeable:
        rejection_reasons.append(f"market_quality:{quality.reason}")

    # Score formula
    hold_hours = max(pnl.expected_hold_hours, 0.5)
    raw_score = (
        pnl.net_profit
        * pricing.confidence
        * pricing.liquidity_score
        * pricing.sell_probability_24h
        / hold_hours
    )

    # Normalize to human-readable range
    raw_score = raw_score / 1_000_000_000

    passes = len(rejection_reasons) == 0 and raw_score > SCORE_THRESHOLD

    return OpportunityScore(
        raw_score=raw_score,
        expected_profit=pnl.net_profit,
        confidence=pricing.confidence,
        liquidity_score=pricing.liquidity_score,
        fill_probability=pricing.sell_probability_24h,
        expected_hold_hours=hold_hours,
        passes_threshold=passes,
        rejection_reasons=rejection_reasons,
    )
