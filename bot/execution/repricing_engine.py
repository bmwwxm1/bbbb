"""Dynamic repricing with hysteresis and anti-thrashing.

Rules:
  - Minimum interval between reprices (default 2h)
  - Maximum reprices per day (default 5)
  - Hysteresis: don't reprice if change < threshold (default 3%)
  - Max loss stop: cancel listing if loss would exceed threshold

Repricing factors:
  - Current floor vs listing price
  - Listing age (older = more aggressive)
  - Undercut pressure (how many new listings appeared below)
  - Orderbook bids (if good bids, hold price)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bot.models.database import Deal
from bot.models.types import MarketSnapshot, nanoton_to_ton

logger = logging.getLogger(__name__)


class RepricingEngine:
    def __init__(
        self,
        *,
        min_interval_hours: float = 2.0,
        max_reprices_per_day: int = 5,
        hysteresis_pct: float = 3.0,
        max_loss_pct: float = 20.0,
        aggressive_after_hours: float = 48.0,
    ) -> None:
        self._min_interval = timedelta(hours=min_interval_hours)
        self._max_per_day = max_reprices_per_day
        self._hysteresis = hysteresis_pct
        self._max_loss = max_loss_pct
        self._aggressive_hours = aggressive_after_hours

    def should_reprice(
        self,
        deal: Deal,
        snapshot: MarketSnapshot,
    ) -> tuple[bool, int | None, str]:
        """Check if a listed deal should be repriced.

        Returns (should_reprice, new_price_or_none, reason).
        """
        now = datetime.now(timezone.utc)
        current_price = deal.sell_price or deal.target_sell_price or 0
        if current_price <= 0:
            return False, None, "no_current_price"

        # Anti-thrashing: max reprices per day
        if deal.reprice_count >= self._max_per_day:
            return False, None, f"max_reprices:{deal.reprice_count}>={self._max_per_day}"

        # Minimum interval
        if deal.last_repriced_at:
            since = now - deal.last_repriced_at
            if since < self._min_interval:
                return (
                    False,
                    None,
                    f"too_soon:{since.total_seconds() / 3600:.1f}h"
                    f"<{self._min_interval.total_seconds() / 3600:.1f}h",
                )

        # Calculate new price
        new_price = self._calculate_price(deal, snapshot, now)
        if new_price is None:
            return False, None, "no_new_price"

        # Hysteresis: skip small changes
        change_pct = abs(new_price - current_price) / current_price * 100
        if change_pct < self._hysteresis:
            return False, None, f"hysteresis:{change_pct:.1f}%<{self._hysteresis}%"

        # Max loss protection
        loss_pct = (deal.buy_price - new_price) / deal.buy_price * 100
        if loss_pct > self._max_loss:
            logger.warning(
                "Repricing would exceed max loss: deal=%d loss=%.1f%%",
                deal.id,
                loss_pct,
            )
            return False, None, f"max_loss:{loss_pct:.1f}%>{self._max_loss}%"

        reason = (
            f"floor_adjusted:{nanoton_to_ton(current_price):.2f}→{nanoton_to_ton(new_price):.2f}TON"
        )
        return True, new_price, reason

    def _calculate_price(
        self,
        deal: Deal,
        snapshot: MarketSnapshot,
        now: datetime,
    ) -> int | None:
        """Calculate optimal new price based on market conditions."""
        floor = snapshot.floor_price
        if floor <= 0:
            return None

        best_bid = snapshot.best_bid
        current_price = deal.sell_price or deal.target_sell_price or 0

        # Listing age
        listed_at = deal.listed_at or deal.detected_at
        age_hours = (now - listed_at).total_seconds() / 3600

        # Undercut count: listings below our price
        undercut_count = sum(1 for item in snapshot.listings if item.price < current_price)

        # Base: slightly above floor
        margin_pct = 0.02  # 2% above floor

        # Age-based adjustment
        if age_hours > self._aggressive_hours:
            margin_pct = 0.0  # at floor
        elif age_hours > 24:
            margin_pct = 0.01  # 1% above floor

        # Undercut pressure
        if undercut_count > 5:
            margin_pct = max(margin_pct - 0.01, 0.0)

        # Best bid consideration — don't go below strong bids
        floor_based = int(floor * (1 + margin_pct))
        if best_bid > 0:
            new_price = max(floor_based, best_bid)
        else:
            new_price = floor_based

        # Never increase price (only decrease)
        if new_price >= current_price:
            return None

        return new_price
