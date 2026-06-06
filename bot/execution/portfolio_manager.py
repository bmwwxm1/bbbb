"""Portfolio manager — tracks holdings, PnL, and inventory health.

Provides aggregate views of:
  - Current inventory (active deals)
  - Realized PnL (completed deals)
  - Unrealized PnL (based on current market prices)
  - Inventory aging distribution
  - Collection concentration
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from bot.models.database import Deal, async_session
from bot.models.types import (
    TERMINAL_STATES,
    DealState,
    nanoton_to_ton,
)

logger = logging.getLogger(__name__)


class PortfolioManager:
    async def get_holdings(self) -> list[dict[str, Any]]:
        """Get all active (non-terminal) deals."""
        active_states = [s.value for s in DealState if s not in TERMINAL_STATES]
        async with async_session() as session:
            result = await session.execute(
                select(Deal)
                .where(Deal.state.in_(active_states), Deal.is_shadow.is_(False))
                .order_by(Deal.detected_at.desc())
            )
            deals = result.scalars().all()

        now = datetime.now(timezone.utc)
        return [
            {
                "id": d.id,
                "collection": d.collection_name,
                "gift_id": d.gift_id,
                "state": d.state,
                "buy_price_ton": nanoton_to_ton(d.buy_price),
                "target_sell_ton": nanoton_to_ton(d.target_sell_price)
                if d.target_sell_price
                else None,
                "age_hours": (now - d.detected_at).total_seconds() / 3600,
                "strategy": d.strategy,
            }
            for d in deals
        ]

    async def get_realized_pnl(self, days: int = 30) -> dict[str, Any]:
        """Aggregate realized PnL over given period."""
        since = datetime.now(timezone.utc) - timedelta(days=days)

        async with async_session() as session:
            result = await session.execute(
                select(Deal).where(
                    Deal.state == DealState.SOLD.value,
                    Deal.sold_at >= since,
                    Deal.is_shadow.is_(False),
                )
            )
            sold_deals = result.scalars().all()

        total_profit = 0
        total_cost = 0
        winners = 0
        losers = 0

        for d in sold_deals:
            profit = d.actual_net_profit or 0
            total_profit += profit
            total_cost += d.buy_price
            if profit > 0:
                winners += 1
            else:
                losers += 1

        win_rate = winners / (winners + losers) if (winners + losers) > 0 else 0

        return {
            "period_days": days,
            "total_deals": len(sold_deals),
            "total_profit_ton": nanoton_to_ton(total_profit),
            "total_cost_ton": nanoton_to_ton(total_cost),
            "roi_pct": (total_profit / total_cost * 100) if total_cost > 0 else 0,
            "winners": winners,
            "losers": losers,
            "win_rate_pct": win_rate * 100,
        }

    async def get_inventory_health(self) -> dict[str, Any]:
        """Inventory aging and concentration analysis."""
        holdings = await self.get_holdings()
        datetime.now(timezone.utc)

        if not holdings:
            return {
                "total_items": 0,
                "total_exposure_ton": 0,
                "aging": {},
                "by_collection": {},
            }

        total_exposure = sum(h["buy_price_ton"] for h in holdings)

        # Aging buckets
        aging: dict[str, int] = {"<6h": 0, "6-24h": 0, "24-48h": 0, "48h+": 0}
        for h in holdings:
            age = h["age_hours"]
            if age < 6:
                aging["<6h"] += 1
            elif age < 24:
                aging["6-24h"] += 1
            elif age < 48:
                aging["24-48h"] += 1
            else:
                aging["48h+"] += 1

        # Collection concentration
        by_coll: dict[str, dict[str, Any]] = {}
        for h in holdings:
            coll = h["collection"]
            if coll not in by_coll:
                by_coll[coll] = {"items": 0, "exposure_ton": 0}
            by_coll[coll]["items"] += 1
            by_coll[coll]["exposure_ton"] += h["buy_price_ton"]

        return {
            "total_items": len(holdings),
            "total_exposure_ton": total_exposure,
            "aging": aging,
            "by_collection": by_coll,
        }

    async def cleanup_stale_deals(
        self,
        mrkt_client: Any = None,
        owner_tg_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Find active deals and close ones where the gift is no longer held.

        Checks MRKT listings to verify if gifts are still on the account.
        Returns list of closed deals for notification.
        """
        holdings = await self.get_holdings()
        if not holdings:
            return []

        # Get current MRKT gifts to check against
        mrkt_gift_ids: set[str] = set()
        if mrkt_client and owner_tg_id:
            try:
                my_gifts = await mrkt_client.get_my_gifts(owner_tg_id, count=100)
                mrkt_gift_ids = {g.get("id", "") for g in my_gifts if g.get("id")}
            except Exception as e:
                logger.warning("Stale deal check: MRKT gifts fetch failed: %s", e)

        closed: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        async with async_session() as session:
            for h in holdings:
                deal_id = h["id"]
                gift_id = h["gift_id"]
                age_hours = h["age_hours"]
                state = h["state"]

                should_close = False
                reason = ""

                # If deal is in BOUGHT/LISTED/etc and gift is not in MRKT inventory
                if (
                    state
                    in (
                        DealState.BOUGHT.value,
                        DealState.WITHDRAWN.value,
                        DealState.AWAITING_TRANSFER.value,
                        DealState.TRANSFER_CONFIRMED.value,
                        DealState.LISTING.value,
                        DealState.LISTED.value,
                        DealState.RELISTING.value,
                        DealState.MONITORING_GETGEMS.value,
                    )
                    and mrkt_gift_ids
                    and gift_id not in mrkt_gift_ids
                    and age_hours > 1.0
                ):
                    should_close = True
                    reason = "gift not found on MRKT account"

                # Very old deals (>72h) with no state change — likely sold manually
                if (
                    not should_close
                    and age_hours > 72
                    and state
                    in (
                        DealState.BOUGHT.value,
                        DealState.LISTED.value,
                        DealState.RELISTING.value,
                    )
                ):
                    should_close = True
                    reason = f"stale ({age_hours:.0f}h without state change)"

                if should_close:
                    result = await session.execute(select(Deal).where(Deal.id == deal_id))
                    deal = result.scalar_one_or_none()
                    if deal and deal.state not in (
                        DealState.SOLD.value,
                        DealState.CANCELLED.value,
                        DealState.HARD_FAILED.value,
                    ):
                        deal.state = DealState.SOLD.value
                        deal.state_updated_at = now
                        deal.sold_at = now
                        deal.last_error = f"auto-closed: {reason}"
                        closed.append(
                            {
                                "collection": h["collection"],
                                "gift_id": gift_id,
                                "buy_price_ton": h["buy_price_ton"],
                                "reason": reason,
                            }
                        )
                        logger.info(
                            "Stale deal closed: %s %s (%s)",
                            h["collection"],
                            gift_id,
                            reason,
                        )

            if closed:
                await session.commit()

        return closed

    async def get_failed_deals_today(self) -> dict[str, int]:
        """Count of soft vs hard failures today."""
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        async with async_session() as session:
            soft = await session.execute(
                select(func.count())
                .select_from(Deal)
                .where(
                    Deal.state == DealState.SOFT_FAILED.value,
                    Deal.state_updated_at >= today,
                )
            )
            hard = await session.execute(
                select(func.count())
                .select_from(Deal)
                .where(
                    Deal.state == DealState.HARD_FAILED.value,
                    Deal.state_updated_at >= today,
                )
            )

        return {
            "soft_failures": soft.scalar() or 0,
            "hard_failures": hard.scalar() or 0,
        }
