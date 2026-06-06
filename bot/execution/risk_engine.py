"""Risk engine — inventory limits, correlation exposure, adaptive tightening.

Checks before every buy:
  1. Total exposure limit
  2. Per-collection exposure (scaled by liquidity)
  3. Correlation group exposure
  4. Total items limit
  5. Items per collection limit
  6. Stuck items check (pause if too many aged items)
  7. Daily loss limit

Adaptive: limits auto-tighten when conditions worsen.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from bot.models.database import Deal, async_session
from bot.models.types import NANOTON, DealState, nanoton_to_ton

logger = logging.getLogger(__name__)

# Correlation groups: collections that move together
DEFAULT_CORRELATION_GROUPS: dict[str, list[str]] = {
    "food": ["Vice Cream", "Candy Cane", "Cookie"],
    "luxury": ["Diamond Ring", "Gold Bar"],
}


class RiskEngine:
    def __init__(
        self,
        *,
        max_total_exposure: int = int(600 * NANOTON),
        max_per_collection: int = int(600 * NANOTON),
        max_items_total: int = 50,
        max_items_per_collection: int = 10,
        max_category_exposure: int = int(600 * NANOTON),
        max_holding_hours: int = 168,
        max_stuck_before_pause: int = 20,
        daily_loss_limit: int = int(50 * NANOTON),
        correlation_groups: dict[str, list[str]] | None = None,
    ) -> None:
        self._max_total = max_total_exposure
        self._max_per_coll = max_per_collection
        self._max_items = max_items_total
        self._max_items_coll = max_items_per_collection
        self._max_category = max_category_exposure
        self._max_hold_hours = max_holding_hours
        self._max_stuck = max_stuck_before_pause
        self._daily_loss = daily_loss_limit
        self._groups = correlation_groups or DEFAULT_CORRELATION_GROUPS

        # Adaptive multiplier (tightens when things go bad)
        self._tightening_factor = 1.0  # 1.0 = normal, 0.5 = half limits

    async def can_buy(
        self, collection: str, price: int, liquidity_score: float = 0.5
    ) -> tuple[bool, str]:
        """Check all risk limits. Returns (allowed, reason)."""

        exposure = await self._get_current_exposure()

        # 1. Total exposure
        effective_max = int(self._max_total * self._tightening_factor)
        if exposure["total"] + price > effective_max:
            return (
                False,
                f"total_exposure:{nanoton_to_ton(exposure['total']):.1f}+{nanoton_to_ton(price):.1f}>{nanoton_to_ton(effective_max):.1f}TON",
            )

        # 2. Per-collection
        max_coll = int(self._max_per_coll * self._tightening_factor)
        coll_exposure = exposure["by_collection"].get(collection, 0)
        if coll_exposure + price > max_coll:
            return (
                False,
                f"collection_exposure:{collection}:{nanoton_to_ton(coll_exposure):.1f}>{nanoton_to_ton(max_coll):.1f}TON",
            )

        # 3. Correlation group
        group = self._find_group(collection)
        if group:
            group_exposure = sum(exposure["by_collection"].get(c, 0) for c in self._groups[group])
            max_group = int(self._max_category * self._tightening_factor)
            if group_exposure + price > max_group:
                return (
                    False,
                    f"group_exposure:{group}:{nanoton_to_ton(group_exposure):.1f}>{nanoton_to_ton(max_group):.1f}TON",
                )

        # 4. Total items
        max_items = int(self._max_items * self._tightening_factor)
        if exposure["total_items"] >= max_items:
            return False, f"total_items:{exposure['total_items']}>={max_items}"

        # 5. Items per collection
        max_items_coll = int(self._max_items_coll * self._tightening_factor)
        coll_items = exposure["items_by_collection"].get(collection, 0)
        if coll_items >= max_items_coll:
            return False, f"collection_items:{collection}:{coll_items}>={max_items_coll}"

        # 6. Stuck items
        if exposure["stuck_items"] >= self._max_stuck:
            return False, f"stuck_items:{exposure['stuck_items']}>={self._max_stuck}"

        # 7. Daily losses
        if exposure["daily_losses"] >= self._daily_loss:
            return False, f"daily_loss_limit:{nanoton_to_ton(exposure['daily_losses']):.1f}TON"

        return True, "ok"

    def tighten(self, factor: float) -> None:
        """Reduce all limits by factor (0.0-1.0). Called when conditions worsen."""
        self._tightening_factor = max(min(factor, 1.0), 0.1)
        logger.warning("Risk tightened to %.0f%%", self._tightening_factor * 100)

    def relax(self) -> None:
        """Return to normal limits."""
        self._tightening_factor = 1.0
        logger.info("Risk limits relaxed to 100%%")

    async def get_status(self) -> dict[str, Any]:
        exposure = await self._get_current_exposure()
        return {
            "tightening_factor": self._tightening_factor,
            "total_exposure_ton": nanoton_to_ton(exposure["total"]),
            "total_items": exposure["total_items"],
            "stuck_items": exposure["stuck_items"],
            "daily_losses_ton": nanoton_to_ton(exposure["daily_losses"]),
            "by_collection": {k: nanoton_to_ton(v) for k, v in exposure["by_collection"].items()},
        }

    # ── Internals ──────────────────────────────────────────────────────

    def _find_group(self, collection: str) -> str | None:
        for group, members in self._groups.items():
            if collection in members:
                return group
        return None

    async def _get_current_exposure(self) -> dict[str, Any]:
        """Query current inventory from DB. Source of truth = Postgres."""
        now = datetime.now(timezone.utc)
        active_states = [
            DealState.BOUGHT.value,
            DealState.WITHDRAWING.value,
            DealState.WITHDRAWN.value,
            DealState.AWAITING_TRANSFER.value,
            DealState.TRANSFER_CONFIRMED.value,
            DealState.MONITORING_GETGEMS.value,
            DealState.LISTING.value,
            DealState.LISTED.value,
            DealState.RELISTING.value,
        ]

        async with async_session() as session:
            # Active deals
            result = await session.execute(
                select(
                    Deal.collection_name,
                    Deal.buy_price,
                    Deal.detected_at,
                    Deal.state,
                ).where(Deal.state.in_(active_states), Deal.is_shadow.is_(False))
            )
            rows = result.all()

        total = 0
        by_collection: dict[str, int] = {}
        items_by_collection: dict[str, int] = {}
        stuck = 0

        for coll, price, detected, state in rows:
            total += price
            by_collection[coll] = by_collection.get(coll, 0) + price
            items_by_collection[coll] = items_by_collection.get(coll, 0) + 1

            try:
                if detected.tzinfo is None:
                    from datetime import timezone as _tz

                    detected = detected.replace(tzinfo=_tz.utc)
                age_hours = (now - detected).total_seconds() / 3600
            except (TypeError, AttributeError):
                age_hours = 0
            if age_hours > self._max_hold_hours:
                stuck += 1

        # Daily losses (hard failures today)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with async_session() as session:
            result = await session.execute(
                select(func.coalesce(func.sum(Deal.buy_price), 0)).where(
                    Deal.state == DealState.HARD_FAILED.value,
                    Deal.state_updated_at >= today_start,
                    Deal.is_shadow.is_(False),
                )
            )
            daily_losses = result.scalar() or 0

        return {
            "total": total,
            "by_collection": by_collection,
            "total_items": len(rows),
            "items_by_collection": items_by_collection,
            "stuck_items": stuck,
            "daily_losses": daily_losses,
        }
