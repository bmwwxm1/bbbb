"""Price matrix builder and deal analyzer for MRKT gifts."""

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any

from bot.config import settings
from bot.fees import (
    cross_market_profit,
    cross_market_roi,
    within_mrkt_profit,
    within_mrkt_roi,
)
from bot.mrkt_client import MRKTClient, nanoton_to_ton

logger = logging.getLogger(__name__)


@dataclass
class ComboKey:
    collection: str
    model: str = ""
    backdrop: str = ""
    symbol: str = ""

    def __hash__(self) -> int:
        return hash((self.collection, self.model, self.backdrop, self.symbol))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ComboKey):
            return False
        return (
            self.collection == other.collection
            and self.model == other.model
            and self.backdrop == other.backdrop
            and self.symbol == other.symbol
        )

    @property
    def display(self) -> str:
        parts = [self.collection]
        if self.model:
            parts.append(self.model)
        if self.backdrop:
            parts.append(self.backdrop)
        if self.symbol:
            parts.append(self.symbol)
        return " / ".join(parts)


@dataclass
class ComboStats:
    floor_price: int = 0
    avg_price: float = 0.0
    median_price: float = 0.0
    prices: list[int] = field(default_factory=list)
    top_buy_order: int = 0
    order_id: str = ""
    order_quantity: int = 0
    listings_count: int = 0
    sales_count: int = 0


@dataclass
class DealSignal:
    gift_id: str
    gift_data: dict[str, Any]
    combo: ComboKey
    listing_price: int
    combo_floor: int
    combo_avg: float
    combo_median: float
    order_price: int
    order_id: str
    signal_type: str  # "cross_market" | "mrkt_arbitrage" | "deep_discount"
    roi_percent: float
    potential_profit: int
    getgems_sell_price: int = 0  # target price on Getgems (for cross-market)

    @property
    def listing_price_ton(self) -> float:
        return nanoton_to_ton(self.listing_price)

    @property
    def combo_floor_ton(self) -> float:
        return nanoton_to_ton(self.combo_floor)

    @property
    def order_price_ton(self) -> float:
        return nanoton_to_ton(self.order_price)

    @property
    def profit_ton(self) -> float:
        return nanoton_to_ton(self.potential_profit)


def _combo_from_gift(gift: dict[str, Any]) -> ComboKey:
    return ComboKey(
        collection=gift.get("collectionName", ""),
        model=gift.get("modelName", ""),
        backdrop=gift.get("backdropName", ""),
        symbol=gift.get("symbolName", ""),
    )


def _best_order_price(order: dict[str, Any]) -> int:
    """Extract the best (max) price from an order in nanoTON."""
    price = order.get("priceMaxNanoTONs", 0)
    if price > 0:
        return price
    price = order.get("pricePerGift", order.get("price", 0))
    return price


class Analyzer:
    """Builds price matrix and finds profitable deals."""

    def __init__(self, client: MRKTClient) -> None:
        self.client = client
        self.price_matrix: dict[ComboKey, ComboStats] = {}
        self.orders_by_combo: dict[ComboKey, list[dict[str, Any]]] = {}
        self.collections: list[dict[str, Any]] = []
        self._seen_gift_ids: set[str] = set()
        self._seen_max = 10_000
        self.getgems_floors: dict[str, int] = {}  # collection_name -> floor nanoTON

    def set_getgems_floors(self, floors: dict[str, int]) -> None:
        self.getgems_floors = floors

    async def load_collections(self) -> list[dict[str, Any]]:
        raw = await self.client.get_collections()
        self.collections = [c for c in raw if not c.get("isHidden", False)]
        logger.info("Loaded %d visible collections", len(self.collections))
        return self.collections

    async def load_orders(self) -> None:
        top_orders = await self.client.get_all_collection_top_orders()
        self.orders_by_combo.clear()
        for order in top_orders:
            coll_name = order.get("collectionName", "")
            order.get("collectionTitle", coll_name)
            combo = ComboKey(
                collection=coll_name,
                model=order.get("modelName") or "",
                backdrop=order.get("backdropName") or "",
                symbol=order.get("symbolName") or "",
            )
            self.orders_by_combo.setdefault(combo, []).append(order)

        total_with_price = sum(
            1
            for orders in self.orders_by_combo.values()
            for o in orders
            if _best_order_price(o) > 0
        )
        logger.info(
            "Loaded orders for %d combos (%d with non-zero price)",
            len(self.orders_by_combo),
            total_with_price,
        )

    async def build_price_matrix_for_collection(
        self,
        collection_name: str,
        max_pages: int = 10,
    ) -> None:
        cursor = ""
        all_gifts: list[dict[str, Any]] = []
        pages = 0

        while pages < max_pages:
            data = await self.client.get_listings(
                collection_names=[collection_name],
                count=20,
                cursor=cursor,
                ordering="Price",
                low_to_high=True,
            )
            gifts = data.get("gifts", [])
            if not gifts:
                break
            all_gifts.extend(gifts)
            cursor = data.get("cursor", "")
            pages += 1
            if not cursor:
                break

        combo_prices: dict[ComboKey, list[int]] = {}
        for gift in all_gifts:
            combo = _combo_from_gift(gift)
            price = gift.get("salePrice", 0)
            if price > 0:
                combo_prices.setdefault(combo, []).append(price)

        for combo, prices in combo_prices.items():
            prices.sort()
            orders = self.orders_by_combo.get(combo, [])
            best_order_price = 0
            best_order_id = ""
            best_order_qty = 0
            if orders:
                best = max(orders, key=_best_order_price)
                best_order_price = _best_order_price(best)
                best_order_id = str(best.get("id", ""))
                completed = best.get("completedQuantity", 0)
                total = best.get("totalQuantity", 0)
                best_order_qty = max(total - completed, 0)

            self.price_matrix[combo] = ComboStats(
                floor_price=prices[0],
                avg_price=statistics.mean(prices),
                median_price=statistics.median(prices),
                prices=prices,
                top_buy_order=best_order_price,
                order_id=best_order_id,
                order_quantity=best_order_qty,
                listings_count=len(prices),
                sales_count=len(prices),
            )

        logger.info(
            "Built matrix for '%s': %d combos from %d gifts",
            collection_name,
            len(combo_prices),
            len(all_gifts),
        )

    async def build_full_matrix(self) -> None:
        if not self.collections:
            await self.load_collections()
        await self.load_orders()

        sorted_collections = sorted(
            self.collections, key=lambda c: c.get("volume", 0), reverse=True
        )

        top = sorted_collections[:20]
        for coll in top:
            name = coll.get("name", "")
            if name:
                await self.build_price_matrix_for_collection(name, max_pages=2)

        logger.info(
            "Fast matrix: %d combos from top %d collections",
            len(self.price_matrix),
            len(top),
        )

    def _fallback_stats(self, combo: ComboKey) -> tuple[ComboStats | None, int]:
        exact = self.price_matrix.get(combo)
        if exact and exact.sales_count >= settings.min_confirmed_sales:
            return exact, 0

        if combo.symbol:
            partial = ComboKey(combo.collection, combo.model, combo.backdrop)
            stats = self.price_matrix.get(partial)
            if stats and stats.sales_count >= settings.min_confirmed_sales:
                return stats, 1

        if combo.backdrop:
            partial = ComboKey(combo.collection, combo.model)
            stats = self.price_matrix.get(partial)
            if stats and stats.sales_count >= settings.min_confirmed_sales:
                return stats, 2

        if combo.model:
            partial = ComboKey(combo.collection)
            stats = self.price_matrix.get(partial)
            if stats and stats.sales_count >= settings.min_confirmed_sales:
                return stats, 3

        return None, -1

    async def scan_for_deals(self, collection_name: str) -> list[DealSignal]:
        """Scan cheapest listings for a collection and find deals."""
        deals: list[DealSignal] = []

        data = await self.client.get_listings(
            collection_names=[collection_name],
            count=20,
            cursor="",
            ordering="Price",
            low_to_high=True,
        )
        gifts = data.get("gifts", [])

        for gift in gifts:
            gift_id = gift.get("id", "")
            if gift_id in self._seen_gift_ids:
                continue

            self._seen_gift_ids.add(gift_id)
            combo = _combo_from_gift(gift)
            listing_price = gift.get("salePrice", 0)
            if listing_price <= 0:
                continue

            max_trade = settings.max_trade_amount_nanoton
            if max_trade > 0 and listing_price > max_trade:
                continue

            stats = self.price_matrix.get(combo)
            orders = self.orders_by_combo.get(combo, [])

            best_order_price = 0
            best_order_id = ""
            if orders:
                best = max(orders, key=_best_order_price)
                best_order_price = _best_order_price(best)
                best_order_id = str(best.get("id", ""))

            # Signal 1: Cross-market — buy on MRKT, sell on Getgems
            if settings.cross_market_enabled:
                gg_floor = self.getgems_floors.get(collection_name, 0)
                if gg_floor > 0:
                    profit = cross_market_profit(listing_price, gg_floor)
                    roi = cross_market_roi(listing_price, gg_floor)
                    if roi >= settings.min_roi_percent:
                        deals.append(
                            DealSignal(
                                gift_id=gift_id,
                                gift_data=gift,
                                combo=combo,
                                listing_price=listing_price,
                                combo_floor=stats.floor_price if stats else listing_price,
                                combo_avg=stats.avg_price if stats else 0,
                                combo_median=stats.median_price if stats else 0,
                                order_price=best_order_price,
                                order_id=best_order_id,
                                signal_type="cross_market",
                                roi_percent=roi,
                                potential_profit=profit,
                                getgems_sell_price=gg_floor,
                            )
                        )
                        continue

            # Signal 2: Within-MRKT arbitrage — order price > listing price
            if best_order_price > 0:
                profit = within_mrkt_profit(listing_price, best_order_price)
                roi = within_mrkt_roi(listing_price, best_order_price)
                if roi >= settings.min_roi_percent:
                    deals.append(
                        DealSignal(
                            gift_id=gift_id,
                            gift_data=gift,
                            combo=combo,
                            listing_price=listing_price,
                            combo_floor=stats.floor_price if stats else listing_price,
                            combo_avg=stats.avg_price if stats else 0,
                            combo_median=stats.median_price if stats else 0,
                            order_price=best_order_price,
                            order_id=best_order_id,
                            signal_type="mrkt_arbitrage",
                            roi_percent=roi,
                            potential_profit=profit,
                        )
                    )
                    continue

            # Signal 3: Deep discount — with fallback hierarchy
            fb_stats, fb_level = self._fallback_stats(combo)
            if fb_stats and fb_stats.floor_price > 0:
                threshold = settings.price_drop_threshold
                if fb_level >= 2:
                    threshold = 0.50
                if fb_level >= 3:
                    threshold = 0.60

                median = fb_stats.median_price
                if median > 0:
                    discount = 1 - (listing_price / median)
                else:
                    discount = 0

                if discount >= threshold:
                    sell_target = int(median * 0.9) if median > 0 else fb_stats.floor_price
                    profit = sell_target - listing_price
                    roi = (profit / listing_price) * 100 if listing_price > 0 else 0
                    if roi >= settings.min_roi_percent:
                        deals.append(
                            DealSignal(
                                gift_id=gift_id,
                                gift_data=gift,
                                combo=combo,
                                listing_price=listing_price,
                                combo_floor=fb_stats.floor_price,
                                combo_avg=fb_stats.avg_price,
                                combo_median=fb_stats.median_price,
                                order_price=best_order_price,
                                order_id=best_order_id,
                                signal_type="deep_discount",
                                roi_percent=roi,
                                potential_profit=profit,
                            )
                        )

        return deals

    def clear_seen(self) -> None:
        if len(self._seen_gift_ids) > self._seen_max:
            self._seen_gift_ids.clear()
