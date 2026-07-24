"""Auto-trader: buys, withdraws, and sells gifts across marketplaces."""

import asyncio
import datetime
import logging
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select

from bot.analyzer import DealSignal
from bot.config import settings
from bot.database import Deal, async_session
from bot.getgems_client import GetgemsClient
from bot.mrkt_client import MRKTClient, nanoton_to_ton, round_price

logger = logging.getLogger(__name__)


class Trader:
    """Executes buy/sell operations across MRKT and Getgems."""

    def __init__(
        self,
        client: MRKTClient,
        getgems: GetgemsClient | None = None,
    ) -> None:
        self.client = client
        self.getgems = getgems
        self._daily_loss = 0
        self._daily_loss_reset: datetime.date = datetime.date.today()

    async def _check_daily_loss_limit(self) -> bool:
        today = datetime.date.today()
        if today != self._daily_loss_reset:
            self._daily_loss = 0
            self._daily_loss_reset = today

        if settings.daily_loss_limit_percent <= 0:
            return True

        balance_data = await self.client.get_balance()
        if not balance_data:
            logger.warning("Cannot get balance, skipping trade")
            return False

        balance = balance_data.get("hard", 0)
        if balance <= 0:
            logger.warning("Balance is 0, skipping trade")
            return False

        max_loss = int(balance * settings.daily_loss_limit_percent / 100)
        if self._daily_loss >= max_loss:
            logger.warning(
                "Daily loss limit reached: %s TON (limit: %s TON)",
                nanoton_to_ton(self._daily_loss),
                nanoton_to_ton(max_loss),
            )
            return False

        return True

    async def execute_deal(self, deal: DealSignal) -> dict[str, Any] | None:
        """Buy a gift and route it based on deal type."""
        if not await self._check_daily_loss_limit():
            return None

        logger.info(
            "Executing deal: %s | %s | price=%.2f TON | ROI=%.1f%% | type=%s",
            deal.gift_id,
            deal.combo.display,
            deal.listing_price_ton,
            deal.roi_percent,
            deal.signal_type,
        )

        # Step 1: Buy the gift on MRKT
        prices = {deal.gift_id: deal.listing_price}
        buy_result = await self.client.buy_gifts([deal.gift_id], prices)
        if not buy_result:
            logger.error("Failed to buy gift %s", deal.gift_id)
            return None

        if isinstance(buy_result, list):
            purchased = buy_result
        elif isinstance(buy_result, dict):
            purchased = buy_result.get("purchasedGifts", [deal.gift_id])
        else:
            purchased = [deal.gift_id]

        if not purchased:
            logger.warning("Gift %s was not purchased (already sold?)", deal.gift_id)
            return None

        logger.info("Bought gift %s for %.2f TON", deal.gift_id, deal.listing_price_ton)

        # Record in DB
        db_deal = Deal(
            gift_id=deal.gift_id,
            collection_name=deal.combo.collection,
            model_name=deal.combo.model,
            backdrop_name=deal.combo.backdrop,
            symbol_name=deal.combo.symbol,
            buy_price=deal.listing_price,
            signal_type=deal.signal_type,
            target_sell_price=deal.getgems_sell_price or deal.order_price,
            status="bought",
        )

        async with async_session() as session:
            session.add(db_deal)
            await session.commit()
            deal_id = db_deal.id

        # Route based on deal type
        if deal.signal_type == "cross_market":
            return await self._handle_cross_market(deal, deal_id)
        elif deal.signal_type == "mrkt_arbitrage":
            return await self._handle_mrkt_arbitrage(deal, deal_id)
        else:
            return await self._handle_deep_discount(deal, deal_id)

    async def _handle_cross_market(
        self,
        deal: DealSignal,
        deal_id: int,
    ) -> dict[str, Any]:
        """Cross-market: buy on MRKT → withdraw to TG → user transfers → list on Getgems."""
        # Step 2: Withdraw from MRKT to Telegram
        logger.info("Withdrawing gift %s from MRKT to Telegram...", deal.gift_id)
        withdraw_result = await self.client.withdraw_gift(deal.gift_id)

        if withdraw_result:
            logger.info("Withdrawn gift %s to Telegram", deal.gift_id)
            async with async_session() as session:
                db_deal = await session.get(Deal, deal_id)
                if db_deal:
                    db_deal.status = "withdrawn"
                    await session.commit()

            # Send transfer notification with confirmation button
            from bot.telegram_bot import send_transfer_request

            await send_transfer_request(
                deal_id=deal_id,
                gift_id=deal.gift_id,
                collection_name=deal.combo.collection,
                model_name=deal.combo.model,
                buy_price=deal.listing_price,
                target_sell_price=deal.getgems_sell_price,
            )

            return {
                "action": "withdrawn",
                "deal_id": deal_id,
                "gift_id": deal.gift_id,
                "collection": deal.combo.display,
                "buy_price": deal.listing_price,
                "target_sell_price": deal.getgems_sell_price,
                "needs_transfer": True,
            }
        else:
            logger.warning("Withdraw failed, will list on MRKT instead")
            return await self._handle_mrkt_sell(deal, deal_id)

    async def _handle_mrkt_arbitrage(
        self,
        deal: DealSignal,
        deal_id: int,
    ) -> dict[str, Any] | None:
        """Within-MRKT: buy listing → sell into order."""
        logger.info("Waiting 65s for MRKT cooldown before selling %s", deal.gift_id)
        await asyncio.sleep(65)

        if deal.order_id:
            sell_result = await self.client.fill_order(deal.order_id, [deal.gift_id])
            if sell_result:
                profit = deal.order_price - deal.listing_price
                logger.info(
                    "Sold %s into order for %.2f TON (profit: %.2f TON)",
                    deal.gift_id,
                    nanoton_to_ton(deal.order_price),
                    nanoton_to_ton(profit),
                )
                async with async_session() as session:
                    db_deal = await session.get(Deal, deal_id)
                    if db_deal:
                        db_deal.sell_price = deal.order_price
                        db_deal.sell_type = "mrkt_order"
                        db_deal.profit = profit
                        db_deal.roi_percent = deal.roi_percent
                        db_deal.status = "sold"
                        db_deal.sold_at = datetime.datetime.now(datetime.UTC)
                        await session.commit()
                return {"action": "sold_to_order", "profit": profit, "deal_id": deal_id}

        logger.warning("Order fill failed, listing on market")
        return await self._handle_mrkt_sell(deal, deal_id)

    async def _handle_deep_discount(
        self,
        deal: DealSignal,
        deal_id: int,
    ) -> dict[str, Any] | None:
        """Deep discount: buy and relist at higher price on MRKT."""
        logger.info("Waiting 65s for MRKT cooldown before listing %s", deal.gift_id)
        await asyncio.sleep(65)
        return await self._handle_mrkt_sell(deal, deal_id)

    async def _handle_mrkt_sell(
        self,
        deal: DealSignal,
        deal_id: int,
    ) -> dict[str, Any] | None:
        """List gift on MRKT marketplace."""
        from bot.mrkt_client import ton_to_nanoton

        markup = ton_to_nanoton(settings.fixed_markup_ton)

        if deal.order_price > 0:
            sell_price = deal.order_price
        elif deal.combo_median > 0:
            sell_price = int(deal.combo_median * 0.95)
        else:
            sell_price = deal.listing_price + markup

        sell_price = round_price(max(sell_price, 500_000_000))

        sell_result = await self.client.sell_gift(deal.gift_id, sell_price)
        if sell_result:
            logger.info("Listed %s for sale at %.2f TON", deal.gift_id, nanoton_to_ton(sell_price))
            async with async_session() as session:
                db_deal = await session.get(Deal, deal_id)
                if db_deal:
                    db_deal.sell_price = sell_price
                    db_deal.sell_type = "mrkt_market"
                    db_deal.status = "listed"
                    db_deal.listed_at = datetime.datetime.now(datetime.UTC)
                    await session.commit()
            return {"action": "listed", "sell_price": sell_price, "deal_id": deal_id}
        else:
            logger.error("Failed to list %s on market", deal.gift_id)
            return None

    async def list_on_getgems(
        self,
        nft_address: str,
        price_nanoton: int,
        deal_id: int | None = None,
    ) -> bool:
        """List a gift on Getgems (called after user confirms transfer)."""
        if not self.getgems:
            logger.error("Getgems client not configured")
            return False

        result = await self.getgems.list_gift_for_sale(nft_address, price_nanoton)
        if result and deal_id:
            async with async_session() as session:
                db_deal = await session.get(Deal, deal_id)
                if db_deal:
                    db_deal.sell_price = price_nanoton
                    db_deal.sell_type = "getgems"
                    db_deal.status = "listed_getgems"
                    db_deal.listed_at = datetime.datetime.now(datetime.UTC)
                    await session.commit()
        return result

    async def check_unsold_gifts(self) -> list[dict[str, Any]]:
        """Check listed gifts and reduce price if unsold for too long."""
        actions: list[dict[str, Any]] = []
        now = datetime.datetime.now(datetime.UTC)

        async with async_session() as session:
            result = await session.execute(select(Deal).where(Deal.status == "listed"))
            listed_deals = result.scalars().all()

        for deal in listed_deals:
            if not deal.listed_at:
                continue

            hours_listed = (now - deal.listed_at).total_seconds() / 3600

            if hours_listed >= 48:
                new_price = int(deal.sell_price * (1 - settings.unsold_price_drop_48h))  # type: ignore[operator]
                new_price = max(new_price, 500_000_000)
                result = await self.client.change_sale_price(deal.gift_id, new_price)
                if result:
                    async with async_session() as session:
                        db_deal = await session.get(Deal, deal.id)
                        if db_deal:
                            db_deal.sell_price = new_price
                            await session.commit()
                    actions.append(
                        {
                            "gift_id": deal.gift_id,
                            "action": "price_reduced_48h",
                            "new_price": new_price,
                        }
                    )

            elif hours_listed >= 24:
                new_price = int(deal.sell_price * (1 - settings.unsold_price_drop_24h))  # type: ignore[operator]
                new_price = max(new_price, 500_000_000)
                result = await self.client.change_sale_price(deal.gift_id, new_price)
                if result:
                    async with async_session() as session:
                        db_deal = await session.get(Deal, deal.id)
                        if db_deal:
                            db_deal.sell_price = new_price
                            await session.commit()
                    actions.append(
                        {
                            "gift_id": deal.gift_id,
                            "action": "price_reduced_24h",
                            "new_price": new_price,
                        }
                    )

        return actions

    async def get_portfolio_stats(self) -> dict[str, Any]:
        async with async_session() as session:
            total_result = await session.execute(select(sa_func.count(Deal.id)))
            total_deals = total_result.scalar() or 0

            sold_result = await session.execute(
                select(sa_func.count(Deal.id)).where(Deal.status == "sold")
            )
            sold_deals = sold_result.scalar() or 0

            listed_result = await session.execute(
                select(sa_func.count(Deal.id)).where(Deal.status.in_(["listed", "listed_getgems"]))
            )
            listed_deals = listed_result.scalar() or 0

            pending_result = await session.execute(
                select(sa_func.count(Deal.id)).where(
                    Deal.status.in_(["withdrawn", "awaiting_transfer"])
                )
            )
            pending_deals = pending_result.scalar() or 0

            profit_result = await session.execute(
                select(sa_func.sum(Deal.profit)).where(Deal.status == "sold")
            )
            total_profit = profit_result.scalar() or 0

            spent_result = await session.execute(select(sa_func.sum(Deal.buy_price)))
            total_spent = spent_result.scalar() or 0

            avg_roi_result = await session.execute(
                select(sa_func.avg(Deal.roi_percent)).where(Deal.status == "sold")
            )
            avg_roi = avg_roi_result.scalar() or 0

            recent_result = await session.execute(
                select(Deal).order_by(Deal.bought_at.desc()).limit(10)
            )
            recent = recent_result.scalars().all()

        return {
            "total_deals": total_deals,
            "sold_deals": sold_deals,
            "listed_deals": listed_deals,
            "pending_deals": pending_deals,
            "total_profit_ton": nanoton_to_ton(total_profit),
            "total_spent_ton": nanoton_to_ton(total_spent),
            "avg_roi": avg_roi,
            "recent_deals": recent,
        }
