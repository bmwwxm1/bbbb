"""Background scanner — the main trading loop with cross-market arbitrage."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from bot.analyzer import Analyzer
from bot.config import settings
from bot.fees import cross_market_roi
from bot.getgems_client import GetgemsClient
from bot.mrkt_client import MRKTClient, nanoton_to_ton, round_price, ton_to_nanoton
from bot.telegram_bot import (
    format_deal_alert,
    format_trade_result,
    is_scanner_running,
    send_alert,
)
from bot.trader import Trader

logger = logging.getLogger(__name__)

ORDER_TTL_SECONDS = 300  # 5 minutes


@dataclass
class TrackedOrder:
    order_id: str
    collection_title: str
    offer_price: int
    created_at: float = field(default_factory=time.monotonic)


class Scanner:
    """Runs the main scan loop: cross-market + within-MRKT deals."""

    def __init__(
        self,
        analyzer: Analyzer,
        trader: Trader,
        client: MRKTClient,
        getgems: GetgemsClient | None = None,
    ) -> None:
        self.analyzer = analyzer
        self.trader = trader
        self.client = client
        self.getgems = getgems
        self._matrix_built = False
        self._seen_listings: set[str] = set()
        self._active_orders: list[TrackedOrder] = []
        self._available_balance: int = 0
        self._orders_this_cycle: int = 0
        self._max_orders_per_cycle: int = settings.auto_order_max_per_cycle

    async def refresh_data(self) -> None:
        """Refresh collections, orders, Getgems prices, and price matrix."""
        logger.info("Refreshing market data...")
        await self.analyzer.load_collections()
        await self.analyzer.load_orders()

        # Load Getgems floor prices for cross-market comparison
        if self.getgems and settings.cross_market_enabled:
            try:
                collection_names = [
                    c.get("name", "") for c in self.analyzer.collections if c.get("name")
                ]
                gg_floors = await self.getgems.get_floor_prices_bulk(collection_names)
                self.analyzer.set_getgems_floors(gg_floors)
                logger.info("Getgems floors loaded: %d collections", len(gg_floors))
            except Exception as e:
                logger.warning("Failed to load Getgems prices: %s", e)

        await self.analyzer.build_full_matrix()
        self._matrix_built = True
        logger.info(
            "Market data refreshed. Matrix: %d combos, Getgems: %d floors",
            len(self.analyzer.price_matrix),
            len(self.analyzer.getgems_floors),
        )

    async def scan_cycle(self) -> None:
        """One scan cycle: check each collection for deals."""
        if not self._matrix_built:
            await self.refresh_data()

        self._orders_this_cycle = 0
        self._seen_listings.clear()
        balance_data = await self.client.get_balance()
        if balance_data:
            self._available_balance = balance_data.get("hard", 0)
            logger.info("Balance: %.2f TON", nanoton_to_ton(self._available_balance))

        sorted_collections = sorted(
            self.analyzer.collections,
            key=lambda c: c.get("volume", 0),
            reverse=True,
        )

        for coll in sorted_collections:
            if not is_scanner_running():
                logger.info("Scanner stopped by user")
                return

            name = coll.get("name", "")
            if not name:
                continue

            deals = await self.analyzer.scan_for_deals(name)
            for deal in deals:
                logger.info(
                    "Deal found: %s | %s | ROI=%.1f%% | buy=%.2f → sell=%.2f TON",
                    deal.signal_type,
                    deal.combo.display,
                    deal.roi_percent,
                    deal.listing_price_ton,
                    nanoton_to_ton(deal.getgems_sell_price or deal.order_price),
                )

                await send_alert(format_deal_alert(deal))

                result = await self.trader.execute_deal(deal)
                if result:
                    await send_alert(format_trade_result(deal, result))
                else:
                    await send_alert(f"❌ Не удалось выполнить сделку: {deal.combo.display}")

            # Place within-MRKT orders (only if enabled and profitable)
            if settings.auto_order_enabled:
                await self.place_smart_orders(name)

    async def place_smart_orders(self, collection_name: str) -> None:
        """Place buy orders only when cross-market resale is profitable."""
        if self._orders_this_cycle >= self._max_orders_per_cycle:
            return

        gg_floor = self.analyzer.getgems_floors.get(collection_name, 0)
        if gg_floor <= 0:
            return

        min_price = ton_to_nanoton(settings.auto_order_min_price_ton)

        # Calculate max buy price that still gives min ROI when selling on Getgems
        max_buy_for_profit = 0
        target_roi = settings.min_roi_percent
        # Binary search for max buy price
        low, high = min_price, gg_floor
        while low <= high:
            mid = (low + high) // 2
            roi = cross_market_roi(mid, gg_floor)
            if roi >= target_roi:
                max_buy_for_profit = mid
                low = mid + 1
            else:
                high = mid - 1

        if max_buy_for_profit < min_price:
            return

        # Only place orders within our balance
        if max_buy_for_profit > self._available_balance:
            max_buy_for_profit = self._available_balance

        if max_buy_for_profit < min_price:
            return

        # Round down to MRKT price step
        offer_price = round_price(max_buy_for_profit)
        if offer_price < min_price:
            return

        result = await self.client.create_order(
            collection_name=collection_name,
            price_nanoton=offer_price,
            quantity=1,
        )

        if result:
            order_id = ""
            if isinstance(result, dict):
                order_id = result.get("id", result.get("orderId", ""))
            elif isinstance(result, str):
                order_id = result

            coll_title = collection_name
            for c in self.analyzer.collections:
                if c.get("name") == collection_name:
                    coll_title = c.get("title", collection_name)
                    break

            if order_id:
                self._active_orders.append(
                    TrackedOrder(
                        order_id=order_id,
                        collection_title=coll_title,
                        offer_price=offer_price,
                    )
                )

            self._orders_this_cycle += 1
            self._available_balance -= offer_price
            roi = cross_market_roi(offer_price, gg_floor)
            logger.info(
                "Smart order: %s for %.2f TON (Getgems floor: %.2f, ROI: %.1f%%)",
                coll_title,
                nanoton_to_ton(offer_price),
                nanoton_to_ton(gg_floor),
                roi,
            )
            await send_alert(
                f"📝 <b>Ордер создан</b>\n\n"
                f"📦 {coll_title}\n"
                f"🏷 Предложение: <b>{nanoton_to_ton(offer_price):.2f} TON</b>\n"
                f"🎯 Getgems floor: {nanoton_to_ton(gg_floor):.2f} TON\n"
                f"📈 Ожидаемый ROI: {roi:.1f}%\n"
                f"⏱ Автоотмена через 5 мин"
            )

    async def cancel_expired_orders(self) -> None:
        now = time.monotonic()
        still_active: list[TrackedOrder] = []

        for order in self._active_orders:
            age = now - order.created_at
            if age >= ORDER_TTL_SECONDS:
                result = await self.client.cancel_order(order.order_id)
                offer_ton = nanoton_to_ton(order.offer_price)
                if result is not None:
                    logger.info(
                        "Cancelled order %s (%s, %.2f TON)",
                        order.order_id,
                        order.collection_title,
                        offer_ton,
                    )
                    self._available_balance += order.offer_price
                    await send_alert(
                        f"⏰ <b>Ордер отменён</b> (5 мин)\n"
                        f"📦 {order.collection_title}\n"
                        f"🏷 Было: {offer_ton:.2f} TON"
                    )
                else:
                    await send_alert(
                        f"ℹ️ Ордер {order.collection_title} "
                        f"({offer_ton:.2f} TON) — не отменён (возможно выполнен)"
                    )
            else:
                still_active.append(order)

        self._active_orders = still_active

    async def check_incoming_offers(self) -> None:
        """Auto-fill buy orders that match our listed gifts."""
        gifts = await self.client.get_my_gifts(settings.admin_chat_id)
        listed = [g for g in gifts if g.get("isOnSale", False)]
        if not listed:
            return

        by_collection: dict[str, list[dict]] = {}
        for g in listed:
            coll = g.get("collectionName", "")
            if coll:
                by_collection.setdefault(coll, []).append(g)

        for coll_name, coll_gifts in by_collection.items():
            data = await self.client.get_orders(
                collection_names=[coll_name],
                count=10,
            )
            orders = data.get("orders", [])
            for order in orders:
                if order.get("isMine", False):
                    continue
                completed = order.get("completedQuantity", 0)
                total = order.get("totalQuantity", 0)
                if completed >= total:
                    continue

                order_max_price = order.get("priceMaxNanoTONs", 0)
                order_id = order.get("id", "")

                for gift in coll_gifts:
                    sale_price = gift.get("salePrice", 0)
                    if sale_price <= 0:
                        continue
                    if order_max_price >= sale_price:
                        gift_id = gift.get("id", "")
                        result = await self.client.fill_order(
                            order_id,
                            [gift_id],
                        )
                        sale_ton = nanoton_to_ton(sale_price)
                        offer_ton = nanoton_to_ton(order_max_price)
                        coll_title = gift.get("collectionTitle", coll_name)
                        if result is not None:
                            logger.info(
                                "Auto-filled order %s with %s for %.2f TON",
                                order_id[:12],
                                coll_title,
                                offer_ton,
                            )
                            await send_alert(
                                f"🤝 <b>Оффер принят!</b>\n\n"
                                f"📦 {coll_title}\n"
                                f"💰 Наша цена: {sale_ton:.2f} TON\n"
                                f"🏷 Оффер: {offer_ton:.2f} TON\n"
                                f"✅ Продано"
                            )
                            coll_gifts.remove(gift)
                            break

    async def check_unsold(self) -> None:
        actions = await self.trader.check_unsold_gifts()
        for action in actions:
            new_price = nanoton_to_ton(action.get("new_price", 0))
            await send_alert(f"🔽 Цена снижена: {action['gift_id']} → {new_price:.2f} TON")

    async def _cancel_all_existing_orders(self) -> None:
        data = await self.client.get_my_orders(count=100)
        orders = data.get("orders", []) if isinstance(data, dict) else []
        if not orders:
            return
        logger.info("Cancelling %d leftover orders", len(orders))
        cancelled = 0
        for order in orders:
            oid = order.get("id", "")
            if oid:
                await self.client.cancel_order(oid)
                cancelled += 1
        logger.info("Cancelled %d leftover orders", cancelled)
        if cancelled:
            await send_alert(f"🧹 Отменено {cancelled} старых ордеров")

    async def run_forever(self) -> None:
        logger.info("Scanner starting...")
        await send_alert("🚀 <b>Бот запущен!</b>\nКросс-маркет арбитраж MRKT ↔ Getgems")

        await self._cancel_all_existing_orders()

        for attempt in range(5):
            try:
                await self.refresh_data()
                gg_count = len(self.analyzer.getgems_floors)
                await send_alert(
                    f"📊 Загружено {len(self.analyzer.collections)} коллекций, "
                    f"{len(self.analyzer.price_matrix)} комбо, "
                    f"{gg_count} Getgems цен"
                )
                break
            except Exception as e:
                wait = min(30 * (attempt + 1), 120)
                logger.warning(
                    "Initial load failed (attempt %d): %s. Retry in %ds",
                    attempt + 1,
                    e,
                    wait,
                )
                await asyncio.sleep(wait)

        matrix_refresh_counter = 0
        unsold_check_counter = 0

        while True:
            try:
                if is_scanner_running():
                    await self.scan_cycle()
                    await self.cancel_expired_orders()
                    await self.check_incoming_offers()

                    matrix_refresh_counter += 1
                    mins = settings.matrix_refresh_minutes
                    refresh_every = (mins * 60) // settings.scan_interval_seconds
                    if matrix_refresh_counter >= refresh_every:
                        await self.refresh_data()
                        matrix_refresh_counter = 0

                    unsold_check_counter += 1
                    unsold_every = 3600 // settings.scan_interval_seconds
                    if unsold_check_counter >= unsold_every:
                        await self.check_unsold()
                        unsold_check_counter = 0

                    self.analyzer.clear_seen()

                await asyncio.sleep(settings.scan_interval_seconds)

            except Exception as e:
                logger.exception("Error in scanner loop: %s", e)
                await send_alert(f"⚠️ Ошибка сканера: {e}")
                await asyncio.sleep(30)
