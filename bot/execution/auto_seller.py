"""Auto-seller: periodically checks inventory and lists gifts at the best price.

Compares MRKT floor vs Getgems floor for each gift in inventory.
If a gift is not listed and the price is favorable, auto-lists on MRKT.
Getgems listing requires manual transfer (logs recommendation).

Also monitors Getgems for cheap gifts (below MRKT floor) and auto-buys them,
then notifies user to transfer to MRKT for resale.

Sends Telegram notifications for all actions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Any

from bot.data.market_data_service import _normalize_name

logger = logging.getLogger(__name__)

NANO = 1_000_000_000


class AutoSeller:
    """Background task that manages inventory listings."""

    def __init__(
        self,
        mrkt: Any,
        mds: Any,
        admin_chat_id: int,
        bot: Any = None,
        fragment: Any = None,
        gift_transfer: Any = None,
        min_profit_pct: float = 5.0,
        check_interval: float = 120.0,
        enabled: bool = True,
        shadow_mode: bool = True,
    ) -> None:
        self._mrkt = mrkt
        self._mds = mds
        self._fragment = fragment
        self._gift_transfer = gift_transfer
        self._admin_chat_id = admin_chat_id
        self._bot = bot
        self._min_profit_pct = min_profit_pct
        self._check_interval = check_interval
        self._enabled = enabled
        self._shadow_mode = shadow_mode
        self._running = False

        # Activity log
        self._log: deque[dict[str, Any]] = deque(maxlen=30)
        self._stats = {
            "checks": 0,
            "listed": 0,
            "repriced": 0,
            "skipped": 0,
            "gg_buys": 0,
            "gg_buy_shadow": 0,
            "frag_buys": 0,
            "frag_buy_shadow": 0,
            "portal_buys": 0,
            "portal_buy_shadow": 0,
            "portal_sells": 0,
        }

        # Pending Getgems transfers: {name: {price, withdrawn_at}}
        self._gg_pending: dict[str, dict[str, Any]] = {}
        # Pending MRKT transfers: gifts bought on Getgems, user needs to move to MRKT
        self._mrkt_pending: dict[str, dict[str, Any]] = {}
        # Cooldown: avoid re-buying same gift within 10 min
        self._buy_cooldown: dict[str, float] = {}
        # Recently bought gifts: gift_id -> buy_timestamp (1 min delay before sell/withdraw)
        self._recently_bought: dict[str, float] = {}
        # Pending retries: user clicked "retry after top-up"
        self._retry_queue: dict[
            str, dict[str, Any]
        ] = {}  # key -> {nft_addr, version, item_name, item_price, mrkt_floor}
        # Pending Fragment→TG transfers: slug -> {name, sell_market, target, ...}
        self._pending_transfers: dict[str, dict[str, Any]] = {}
        # Buy prices: collection_name -> buy_price_nanoton (to prevent selling at loss)
        self._buy_prices: dict[str, int] = {}
        # Cross-market buy targets: collection_name -> intended sell market
        self._cross_market_targets: dict[str, str] = {}

        # Market toggles (buy/sell separate)
        self._markets_buy_enabled: dict[str, bool] = {
            "mrkt": True,
            "getgems": True,
            "fragment": True,
            "portal": True,
        }
        self._markets_sell_enabled: dict[str, bool] = {
            "mrkt": True,
            "getgems": True,
            "fragment": True,
            "portal": True,
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, val: bool) -> None:
        self._enabled = val
        logger.info("AutoSeller enabled=%s", val)

    def set_market_enabled(self, market: str, enabled: bool) -> None:
        self._markets_buy_enabled[market] = enabled
        self._markets_sell_enabled[market] = enabled
        logger.info("AutoSeller market %s enabled=%s", market, enabled)

    def set_market_buy_enabled(self, market: str, enabled: bool) -> None:
        self._markets_buy_enabled[market] = enabled
        logger.info("AutoSeller market %s buy=%s", market, enabled)

    def set_market_sell_enabled(self, market: str, enabled: bool) -> None:
        self._markets_sell_enabled[market] = enabled
        logger.info("AutoSeller market %s sell=%s", market, enabled)

    def is_market_enabled(self, market: str) -> bool:
        buy = self._markets_buy_enabled.get(market, True)
        sell = self._markets_sell_enabled.get(market, True)
        return buy or sell

    def is_market_buy_enabled(self, market: str) -> bool:
        return self._markets_buy_enabled.get(market, True)

    def is_market_sell_enabled(self, market: str) -> bool:
        return self._markets_sell_enabled.get(market, True)

    def _log_event(self, event: str, **extra: Any) -> None:
        self._log.append({"time": time.time(), "event": event, **extra})

    async def _notify(self, text: str, reply_markup: Any = None) -> None:
        if self._bot and self._admin_chat_id:
            try:
                await self._bot.send_message(
                    self._admin_chat_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logger.warning("AutoSeller notify failed: %s", e)

    async def check_and_list(self) -> None:
        """Check inventory once and list unlisted gifts at best price."""
        if not self.is_market_sell_enabled("mrkt"):
            return
        self._stats["checks"] += 1

        # Wait until floor caches are populated to avoid wrong market selection
        if self._mds:
            gg_floors = self._mds.get_all_gg_floors()
            portal_floors = self._mds.get_all_portal_floors()
            if not gg_floors and not portal_floors:
                logger.info("AutoSeller MRKT: skipping — floor caches not loaded yet")
                return

        gifts = await self._mrkt.get_my_gifts(
            owner_tg_id=self._admin_chat_id,
            count=100,
        )
        if not gifts:
            logger.info("AutoSeller MRKT: no gifts in inventory")
            return

        unlisted = [g for g in gifts if not (g.get("isOnSale") or g.get("isOnAuction"))]
        listed = len(gifts) - len(unlisted)
        logger.info(
            "AutoSeller MRKT: %d gifts (%d listed, %d unlisted)",
            len(gifts), listed, len(unlisted),
        )

        for gift in gifts:
            # Use isOnSale field (not salePrice which is collection floor)
            is_listed = gift.get("isOnSale") is True or gift.get("isOnAuction") is True
            if is_listed:
                await self._maybe_reprice(gift)
                continue

            await self._try_list(gift)

    def _get_mrkt_sell_price(self, collection_name: str) -> int:
        """Get MRKT sell price: instant (top order) or floor.

        When instant mode is on, uses top buy order but caps at
        1.5× floor to filter stale/manipulative orders.
        Falls back to floor if no valid order.
        """
        from bot.interface.telegram_bot import get_sell_price_mode

        if not self._mds:
            return 0
        floor = self._mds.get_all_mrkt_floors().get(collection_name, 0)
        if get_sell_price_mode("mrkt") != "instant":
            return floor
        top_order = self._mds.get_cached_top_order(collection_name)
        if top_order <= 0:
            return floor
        # Cap at 1.5× floor to avoid stale/fake orders
        if floor > 0 and top_order > floor * 1.5:
            return floor
        return top_order

    async def _get_best_price(self, collection_name: str) -> tuple[str, int]:
        """Determine best sell market and price for a collection.

        Returns (market, sell_price_nanoton).
        Uses instant sell (best buy order) or floor depending on settings.
        Only considers markets where we can actually sell: mrkt, getgems, portal.
        Fragment excluded — no sell support.
        """
        from bot.config import settings as _cfg

        mrkt_price = self._get_mrkt_sell_price(collection_name)
        if mrkt_price <= 0:
            try:
                listings = await self._mrkt.get_listings(
                    collection_names=[collection_name],
                    count=3,
                    ordering="Price",
                    low_to_high=True,
                )
                items = listings.get("gifts", [])
                if items:
                    mrkt_price = items[0].get("salePrice", 0) or 0
            except Exception as e:
                logger.debug(
                    "_get_best_price MRKT error for %s: %s",
                    collection_name, e,
                )

        gg_floor = 0
        if self._mds:
            gg_floor = self._mds.get_cached_gg_floor(collection_name)

        portal_price = 0
        if self._mds:
            portal_price = self._mds.get_cached_portal_floor(
                collection_name,
            )

        wfees = {
            "mrkt": int(_cfg.mrkt_withdraw_fee_ton * NANO),
            "getgems": int(_cfg.getgems_withdraw_fee_ton * NANO),
            "portal": 0,
        }
        markets = [
            ("mrkt", mrkt_price, mrkt_price - wfees["mrkt"]),
            ("getgems", gg_floor, gg_floor - wfees["getgems"]),
            ("portal", portal_price, portal_price - wfees["portal"]),
        ]
        markets = [
            (m, p, n) for m, p, n in markets
            if n > 0 and self.is_market_sell_enabled(m)
        ]
        if not markets:
            return "", 0

        markets.sort(key=lambda x: x[2], reverse=True)
        best_market, best_price, _ = markets[0]

        return best_market, best_price

    def mark_bought(self, gift_id: str, *, buy_price: int = 0, collection_name: str = "") -> None:
        """Mark a gift as just bought (starts 60s cooldown before sell/withdraw)."""
        self._recently_bought[gift_id] = time.time()
        if buy_price > 0 and collection_name:
            self._buy_prices[collection_name] = buy_price

    async def _try_list(self, gift: dict[str, Any]) -> None:
        """Try to list a gift on the best market."""
        name = gift.get("collectionName", "?")
        gift_id = gift.get("id", "")
        number = gift.get("number", "")

        if not gift_id:
            return

        # 1-minute delay after purchase (MRKT restriction)
        bought_at = self._recently_bought.get(gift_id, 0)
        if bought_at and time.time() - bought_at < 60:
            remaining = int(60 - (time.time() - bought_at))
            logger.info("AutoSeller: %s #%s — waiting %ds after purchase", name, number, remaining)
            return

        market, floor_price = await self._get_best_price(name)
        logger.info(
            "AutoSeller _try_list %s #%s → best=%s floor=%.2f",
            name, number, market, floor_price / NANO if floor_price else 0,
        )

        if not market or floor_price <= 0:
            self._stats["skipped"] += 1
            self._log_event("skip", gift=f"{name} #{number}", reason="нет цены")
            return

        # Protect against selling at a loss
        min_price = self._buy_prices.get(name, 0)

        if market == "mrkt":
            # List slightly below floor for faster sale
            list_price = int(floor_price * 0.98)
            # Round to 0.01 TON (10_000_000 nanoton)
            list_price = (list_price // 10_000_000) * 10_000_000
            list_price = max(list_price, 100_000_000)  # min 0.1 TON
            if min_price > 0:
                list_price = max(list_price, min_price)

            try:
                result = await self._mrkt.sell_gift(gift_id, list_price)
                if result is not None:
                    self._stats["listed"] += 1
                    price_ton = list_price / NANO
                    self._log_event(
                        "listed",
                        gift=f"{name} #{number}",
                        market="MRKT",
                        price=price_ton,
                    )
                    await self._notify(
                        f"🏷 <b>Автопродажа</b>\n\n"
                        f"🎁 {name} #{number}\n"
                        f"📍 MRKT за <b>{price_ton:.2f}</b> TON\n"
                        f"(floor: {floor_price / NANO:.2f} TON)"
                    )
                    logger.info(
                        "AutoSeller: listed %s #%s on MRKT at %.2f TON",
                        name,
                        number,
                        price_ton,
                    )
                else:
                    self._log_event(
                        "error",
                        gift=f"{name} #{number}",
                        reason="MRKT sell_gift failed",
                    )
            except Exception as e:
                logger.error("AutoSeller list error: %s", e)
                self._log_event("error", gift=f"{name} #{number}", reason=str(e))

        elif market in ("getgems", "portal"):
            list_price = int(floor_price * 0.98)
            list_price = max(list_price, 100_000_000)
            if min_price > 0:
                list_price = max(list_price, min_price)
            target_label = "Getgems" if market == "getgems" else "Portal"
            icon = "💎" if market == "getgems" else "🟣"

            # Step 1: Auto-withdraw from MRKT
            try:
                withdraw_result = await self._mrkt.withdraw_gift(gift_id)
                if withdraw_result is None:
                    self._log_event(
                        "error",
                        gift=f"{name} #{number}",
                        reason="MRKT withdraw failed",
                    )
                    return
            except Exception as e:
                logger.error("AutoSeller withdraw error: %s", e)
                return

            self._log_event(
                "withdrawn",
                gift=f"{name} #{number}",
                market=f"MRKT→{target_label}",
            )

            # Step 2: Track pending + notify user
            self._gg_pending[name] = {
                "price": list_price,
                "number": number,
                "withdrawn_at": time.time(),
                "target_market": market,
            }

            await self._notify(
                f"📤 <b>Автовывод с MRKT → {icon} {target_label}</b>\n\n"
                f"🎁 {name} #{number}\n"
                f"{icon} {target_label} floor: "
                f"<b>{floor_price / NANO:.2f}</b> TON\n"
                f"Цена продажи: "
                f"<b>{list_price / NANO:.2f}</b> TON\n\n"
                f"⏳ Бот выставит на {target_label} автоматически."
            )

    async def _maybe_reprice(self, gift: dict[str, Any]) -> None:
        """Check if a listed gift should be repriced."""
        name = gift.get("collectionName", "?")
        gift_id = gift.get("id", "")
        number = gift.get("number", "")
        current_price = gift.get("salePrice", 0) or 0

        if not gift_id or current_price <= 0:
            return

        market, floor_price = await self._get_best_price(name)
        if not market or floor_price <= 0:
            return

        # Only reprice on MRKT (can't reprice on Getgems via API)
        if market != "mrkt":
            return

        # Reprice if our price is >10% above floor (won't sell)
        # or >5% below floor (leaving money on table)
        ratio = current_price / floor_price if floor_price > 0 else 1.0

        if ratio > 1.10:
            # Too expensive — lower price
            new_price = int(floor_price * 0.98)
            new_price = max(new_price, 100_000_000)
            try:
                result = await self._mrkt.change_sale_price(gift_id, new_price)
                if result is not None:
                    self._stats["repriced"] += 1
                    self._log_event(
                        "repriced",
                        gift=f"{name} #{number}",
                        old=current_price / NANO,
                        new=new_price / NANO,
                    )
                    await self._notify(
                        f"💲 <b>Переоценка</b>\n\n"
                        f"🎁 {name} #{number}\n"
                        f"Было: {current_price / NANO:.2f} → Стало: {new_price / NANO:.2f} TON\n"
                        f"(floor: {floor_price / NANO:.2f} TON)"
                    )
            except Exception as e:
                logger.error("AutoSeller reprice error: %s", e)

    async def _check_getgems_pending(self) -> None:
        """Monitor pending transfers and auto-list on target market."""
        if not self._gg_pending:
            return

        gg = self._mds._gg if hasattr(self._mds, "_gg") else None
        portal_c = self._mds._portal if hasattr(self._mds, "_portal") else None

        # Split pending by target market
        gg_items = {
            k: v for k, v in self._gg_pending.items()
            if v.get("target_market", "getgems") == "getgems"
        }
        portal_items = {
            k: v for k, v in self._gg_pending.items()
            if v.get("target_market") == "portal"
        }

        logger.info(
            "Checking pending: %d GG, %d Portal (%s)",
            len(gg_items),
            len(portal_items),
            ", ".join(self._gg_pending.keys()),
        )

        listed_names: list[str] = []

        # --- Portal pending: check Portal owned NFTs ---
        if portal_items and portal_c and portal_c.authenticated:
            try:
                owned = await portal_c.get_owned_nfts()
                owned_nfts = owned.get("nfts", []) if owned else []
            except Exception as e:
                logger.debug("Portal owned check failed: %s", e)
                owned_nfts = []

            for name, info in list(portal_items.items()):
                price = info["price"]
                number = info.get("number", "")

                if time.time() - info["withdrawn_at"] > 7200:
                    self._gg_pending.pop(name, None)
                    await self._notify(
                        f"⏰ Ожидание истекло: {name} #{number}\n"
                        f"Подарок не найден на Portal за 2 часа."
                    )
                    continue

                # Search in Portal owned NFTs
                for nft in owned_nfts:
                    nft_name = nft.get("name", "")
                    if name.lower() in nft_name.lower():
                        nft_id = nft.get("id", "")
                        status = nft.get("status", "")
                        if status == "listed":
                            listed_names.append(name)
                            break
                        if nft_id and status != "listed":
                            price_ton = price / NANO
                            try:
                                ok = await portal_c.list_single(
                                    nft_id, price_ton,
                                )
                                if ok:
                                    listed_names.append(name)
                                    self._stats["listed"] += 1
                                    await self._notify(
                                        f"🟣 <b>Выставлено на Portal</b>\n\n"
                                        f"🎁 {name} #{number}\n"
                                        f"💰 <b>{price_ton:.2f}</b> TON"
                                    )
                            except Exception as e:
                                logger.error(
                                    "Portal list %s failed: %s", name, e,
                                )
                        break

        # --- Getgems pending ---
        if not gg_items:
            for n in listed_names:
                self._gg_pending.pop(n, None)
            return

        if not gg or not gg._keypair or not gg._wallet_address:
            for n in listed_names:
                self._gg_pending.pop(n, None)
            return

        user_gifts: list[dict[str, Any]] = []
        try:
            user_gifts = await gg.get_user_offchain_gifts(gg._wallet_address)
            logger.info("Getgems user gifts: %d found", len(user_gifts))
        except Exception as e:
            logger.debug("Getgems user gifts check failed: %s", e)

        for name, info in list(gg_items.items()):
            price = info["price"]
            number = info.get("number", "")

            if time.time() - info["withdrawn_at"] > 7200:
                self._gg_pending.pop(name, None)
                self._log_event("expired", gift=f"{name} #{number}", reason="2h timeout")
                await self._notify(
                    f"⏰ Ожидание истекло: {name} #{number}\n"
                    f"Подарок не найден на Getgems за 2 часа."
                )
                continue

            found_in_user_gifts = False
            for g in user_gifts:
                g_name = g.get("name", "")
                if name.lower() in g_name.lower() or g_name.lower() in name.lower():
                    nft_addr = g.get("address", "")
                    sale = g.get("sale")
                    if sale:
                        listed_names.append(name)
                        found_in_user_gifts = True
                        break
                    if nft_addr and price > 0:
                        found_in_user_gifts = True
                        await self._try_list_on_getgems(
                            gg,
                            name,
                            number,
                            nft_addr,
                            price,
                            listed_names,
                        )
                    break

            if found_in_user_gifts:
                continue

            if number:
                try:
                    gift = await gg.find_gift_in_collection(name, number)
                    if gift:
                        nft_addr = gift.get("address", "")
                        sale = gift.get("sale")
                        if sale:
                            listed_names.append(name)
                            logger.info("Gift %s #%s already on sale", name, number)
                        elif nft_addr and price > 0:
                            await self._try_list_on_getgems(
                                gg,
                                name,
                                number,
                                nft_addr,
                                price,
                                listed_names,
                            )
                except Exception as e:
                    logger.debug("Collection search for %s #%s: %s", name, number, e)

        for name in listed_names:
            self._gg_pending.pop(name, None)

    async def _try_list_on_getgems(
        self,
        gg: Any,
        name: str,
        number: str,
        nft_addr: str,
        price: int,
        listed_names: list[str],
        *,
        buy_price: int = 0,
    ) -> None:
        """Attempt to list a gift on Getgems.

        If falling_price_enabled, uses Getgems native falling price sale
        (start=price, min=buy_price+fees, step=3%/interval).
        Otherwise, uses fixed price listing.
        """
        from bot.config import settings

        try:
            logger.info("Auto-listing %s #%s at %s on Getgems...", name, number, nft_addr)

            if settings.falling_price_enabled and buy_price > 0:
                success = await self._list_falling_price(
                    gg, nft_addr, price, buy_price, settings
                )
                mode = "FallingPrice"
            else:
                success = await gg.list_offchain_gift_rest(nft_addr, price)
                mode = "FixedPrice"

            if success:
                listed_names.append(name)
                self._stats["listed"] += 1
                self._log_event(
                    "listed",
                    gift=f"{name} #{number}",
                    market=f"Getgems({mode})",
                    price=price / NANO,
                )
                if mode == "FallingPrice":
                    gg_fee_pct = settings.getgems_sell_fee_pct
                    min_price = int(buy_price * (1 + gg_fee_pct / 100) * 1.01)
                    interval_label = gg.FALLING_INTERVALS_MS.get(
                        settings.falling_price_interval_ms,
                        f"{settings.falling_price_interval_ms}ms",
                    )
                    await self._notify(
                        f"📉 <b>Падающая цена на Getgems!</b>\n\n"
                        f"🎁 {name} #{number}\n"
                        f"💰 Старт: <b>{price / NANO:.2f}</b> TON\n"
                        f"🔻 Минимум: <b>{min_price / NANO:.2f}</b> TON\n"
                        f"⏱ Снижение: -{settings.falling_price_decrease_pct:.1f}% "
                        f"каждые {interval_label}\n"
                        f"Выставлено автоматически."
                    )
                else:
                    await self._notify(
                        f"💎 <b>Автопродажа на Getgems!</b>\n\n"
                        f"🎁 {name} #{number}\n"
                        f"📍 Цена: <b>{price / NANO:.2f}</b> TON\n"
                        f"Выставлено автоматически."
                    )
            else:
                logger.warning("Getgems listing failed for %s #%s (%s)", name, number, mode)
        except Exception as e:
            logger.error("Getgems auto-list %s failed: %s", name, e)

    async def _list_falling_price(
        self,
        gg: Any,
        nft_addr: str,
        start_price: int,
        buy_price: int,
        settings: Any,
    ) -> bool:
        """List using Getgems falling price with calculated parameters."""
        gg_fee_pct = settings.getgems_sell_fee_pct
        min_price = int(buy_price * (1 + gg_fee_pct / 100) * 1.01)

        decrease_pct = settings.falling_price_decrease_pct
        decrease_value = int(start_price * decrease_pct / 100)
        if decrease_value <= 0:
            decrease_value = int(0.1 * NANO)

        if min_price >= start_price:
            min_price = int(start_price * 0.95)

        if (start_price - min_price) < decrease_value:
            decrease_value = start_price - min_price

        interval_ms = settings.falling_price_interval_ms

        return await gg.list_offchain_gift_falling_price(
            nft_address=nft_addr,
            start_price_nanoton=start_price,
            min_price_nanoton=min_price,
            decrease_value_nanoton=decrease_value,
            decrease_interval_ms=interval_ms,
        )

    def add_pending_getgems(self, name: str, price: int, number: str = "") -> None:
        """Add a gift to the Getgems monitoring queue (called from UI)."""
        self._gg_pending[name] = {
            "price": price,
            "number": number,
            "withdrawn_at": time.time(),
        }
        logger.info("Added to Getgems pending: %s price=%d", name, price)

    async def _auto_list_unlisted_getgems(self) -> None:
        """Scan user's Getgems wallet, auto-list any unlisted gifts."""
        if not self.is_market_sell_enabled("getgems"):
            return
        gg = self._mds._gg if hasattr(self._mds, "_gg") else None
        if not gg:
            logger.info("AutoSeller GG: no getgems client")
            return
        if not gg._keypair or not gg._wallet_address:
            logger.info(
                "AutoSeller GG: no keypair=%s wallet=%s",
                bool(gg._keypair),
                bool(gg._wallet_address),
            )
            return

        try:
            user_gifts = await gg.get_user_offchain_gifts(gg._wallet_address)
        except Exception as e:
            logger.info("Getgems wallet scan failed: %s", e)
            return

        logger.info("AutoSeller GG: %d gifts in wallet", len(user_gifts) if user_gifts else 0)
        if not user_gifts:
            return

        for gift in user_gifts:
            nft_addr = gift.get("address", "")
            raw_name = gift.get("name", "") or gift.get("collectionName", "?")
            sale = gift.get("sale")

            # Strip "#12345" suffix to get collection name
            match = re.match(r"^(.+?)\s*#\d+$", raw_name)
            name = match.group(1).strip() if match else raw_name
            # Unused expression removed — number extraction not needed here

            logger.info(
                "AutoSeller GG gift: raw=%s coll=%s addr=%s sale=%s",
                raw_name,
                name,
                nft_addr[:40] if nft_addr else "none",
                bool(sale),
            )

            if sale or not nft_addr:
                continue  # already on sale or no address

            # Skip items pending cross-market transfer
            if name in self._mrkt_pending:
                pending = self._mrkt_pending[name]
                sell_market = pending.get("sell_market", "")
                logger.info(
                    "AutoSeller GG: skip %s — pending cross-market sale to %s",
                    raw_name, sell_market,
                )
                continue

            # Try multiple sources for floor price
            gg_floor = 0

            # 1) Cached Getgems floor
            if self._mds:
                gg_floor = self._mds.get_cached_gg_floor(name)

            # 2) Try all Getgems floors (fuzzy match)
            if gg_floor <= 0 and self._mds:
                all_floors = self._mds.get_all_gg_floors()
                name_lower = name.lower()
                for fname, fprice in all_floors.items():
                    if name_lower in fname.lower() or fname.lower() in name_lower:
                        gg_floor = fprice
                        logger.info(
                            "AutoSeller: fuzzy floor match %s → %s = %.2f",
                            name,
                            fname,
                            fprice / NANO,
                        )
                        break

            # 3) Try Getgems collection floor API
            if gg_floor <= 0:
                coll_addr = (
                    gg._find_collection_address(name)
                    if hasattr(gg, "_find_collection_address")
                    else ""
                )
                if coll_addr:
                    try:
                        gg_floor = await gg._get_collection_floor_by_addr(coll_addr)
                    except Exception as e:
                        logger.debug("_get_collection_floor_by_addr error for %s: %s", name, e)

            # 4) Try MRKT floor
            if gg_floor <= 0:
                try:
                    listings = await self._mrkt.get_listings(
                        collection_names=[name],
                        count=3,
                        ordering="Price",
                        low_to_high=True,
                    )
                    items = listings.get("gifts", [])
                    if items:
                        gg_floor = items[0].get("salePrice", 0) or 0
                except Exception as e:
                    logger.debug("MRKT floor fallback error for %s: %s", name, e)

            if gg_floor <= 0:
                logger.info("AutoSeller: skip %s — no floor price (all sources empty)", name)
                continue

            list_price = int(gg_floor * 0.98)
            list_price = max(list_price, 100_000_000)

            # Never list below buy price (prevent losses)
            buy_price_min = self._buy_prices.get(name, 0)
            buy_info = self._mrkt_pending.get(name)
            if buy_info:
                bp = buy_info.get("buy_price", 0)
                if bp > buy_price_min:
                    buy_price_min = bp
            if buy_price_min > 0 and list_price < buy_price_min:
                list_price = buy_price_min
                logger.info(
                    "AutoSeller: raised price to buy_price %.2f for %s",
                    list_price / NANO, raw_name,
                )

            logger.info(
                "AutoSeller: auto-listing %s at %.2f TON (addr=%s)",
                raw_name,
                list_price / NANO,
                nft_addr,
            )

            try:
                from bot.config import settings

                if settings.falling_price_enabled:
                    success = await self._list_falling_price(
                        gg, nft_addr, list_price, 0, settings
                    )
                    mode = "FallingPrice"
                else:
                    success = await gg.list_offchain_gift_rest(nft_addr, list_price)
                    mode = "FixedPrice"

                if success:
                    self._stats["listed"] += 1
                    self._log_event(
                        "listed",
                        gift=raw_name,
                        market=f"Getgems({mode})",
                        price=list_price / NANO,
                    )
                    if mode == "FallingPrice":
                        interval_label = gg.FALLING_INTERVALS_MS.get(
                            settings.falling_price_interval_ms,
                            f"{settings.falling_price_interval_ms}ms",
                        )
                        await self._notify(
                            f"📉 <b>Падающая цена на Getgems!</b>\n\n"
                            f"🎁 {raw_name}\n"
                            f"💰 Старт: <b>{list_price / NANO:.2f}</b> TON\n"
                            f"⏱ Снижение каждые {interval_label}\n"
                            f"Выставлено автоматически."
                        )
                    else:
                        await self._notify(
                            f"💎 <b>Автопродажа на Getgems!</b>\n\n"
                            f"🎁 {raw_name}\n"
                            f"📍 Цена: <b>{list_price / NANO:.2f}</b> TON\n"
                            f"Выставлено автоматически."
                        )
                else:
                    logger.warning("Getgems auto-list failed for %s (addr=%s)", raw_name, nft_addr)
            except Exception as e:
                logger.error("Getgems auto-list %s error: %s", raw_name, e)

    # ── Reverse direction: buy on Getgems, sell on MRKT ──────────────

    async def _check_getgems_buys(self) -> None:
        """Scan Getgems on-sale gifts and buy if cheaper than MRKT/Portal floor.

        Uses cached MRKT/Portal snapshots where available to minimize API calls.
        Only fetches fresh MRKT data for collections with promising spreads.
        """
        if not self.is_market_buy_enabled("getgems"):
            return

        gg = self._mds._gg if hasattr(self._mds, "_gg") else None
        if not gg:
            return

        # Clean expired cooldowns
        now = time.time()
        expired = [k for k, v in self._buy_cooldown.items() if now - v > 600]
        for k in expired:
            self._buy_cooldown.pop(k, None)

        # Get collections with cross-market data
        gg_floors = self._mds.get_all_gg_floors() if hasattr(self._mds, "get_all_gg_floors") else {}
        if not gg_floors:
            return

        from bot.config import settings as _cfg
        from bot.interface.telegram_bot import get_runtime

        min_roi = get_runtime("min_roi_pct") / 100
        pre_filter_threshold = 1.0 - min_roi

        # Sell fees per market (fraction)
        mrkt_fee = _cfg.mrkt_sell_fee_pct / 100
        _portal_fee = 0.0  # Portal has no sell fee

        # (name, gg_floor, net_sell, sell_market)
        candidates: list[tuple[str, int, int, str]] = []
        portal_floors = self._mds.get_all_portal_floors()

        uncached: list[tuple[str, int]] = []
        for coll_name, gg_floor in gg_floors.items():
            if gg_floor <= 0:
                continue

            # Find best sell target between MRKT and Portal (net of fees)
            mrkt_sell = self._get_mrkt_sell_price(coll_name)
            mrkt_net = int(mrkt_sell * (1 - mrkt_fee))
            portal_floor = portal_floors.get(coll_name, 0)
            portal_net = int(portal_floor * (1 - _portal_fee))

            best_net = 0
            best_market = "MRKT"
            if (mrkt_net > 0 and mrkt_net > best_net
                    and self.is_market_sell_enabled("mrkt")):
                best_net = mrkt_net
                best_market = "MRKT"
            if (portal_net > 0 and portal_net > best_net
                    and self.is_market_sell_enabled("portal")):
                best_net = portal_net
                best_market = "Portal"

            if best_net > 0:
                if gg_floor < best_net * pre_filter_threshold:
                    candidates.append(
                        (coll_name, gg_floor, best_net, best_market),
                    )
            elif mrkt_sell == 0:
                uncached.append((coll_name, gg_floor))

        # Fetch up to 10 uncached MRKT floors per cycle
        fetched = 0
        for coll_name, gg_floor in uncached[:10]:
            await asyncio.sleep(5)
            mrkt_floor = await self._mds.fetch_mrkt_floor(coll_name)
            fetched += 1
            mrkt_net_u = int(mrkt_floor * (1 - mrkt_fee))
            if (mrkt_net_u > 0 and self.is_market_sell_enabled("mrkt")
                    and gg_floor < mrkt_net_u * pre_filter_threshold):
                candidates.append(
                    (coll_name, gg_floor, mrkt_net_u, "MRKT"),
                )

        logger.info(
            "Getgems buy scan: %d GG, %d Portal, %d fetched, %d cand",
            len(gg_floors),
            len(portal_floors),
            fetched,
            len(candidates),
        )
        if not candidates:
            return

        # Sort by potential ROI (best first)
        candidates.sort(key=lambda x: x[2] / x[1] if x[1] > 0 else 0, reverse=True)

        checked = 0
        for coll_name, gg_floor, sell_floor, sell_market in candidates[:5]:
            # Verify fresh sell floor (with rate limit pause)
            await asyncio.sleep(10)
            if sell_market == "MRKT":
                try:
                    listings = await self._mrkt.get_listings(
                        collection_names=[coll_name],
                        count=3,
                        ordering="Price",
                        low_to_high=True,
                    )
                    items = listings.get("gifts", [])
                    if items:
                        raw = items[0].get("salePrice", 0) or 0
                        sell_floor = int(raw * (1 - mrkt_fee))
                except Exception as e:
                    logger.debug("MRKT floor verify error for %s: %s", coll_name, e)
                    continue

            sell_icon = "🟦" if sell_market == "MRKT" else "🟣"

            if sell_floor <= 0:
                continue

            if gg_floor >= sell_floor * (1 - min_roi):
                continue

            profit_pct = (sell_floor - gg_floor) / gg_floor * 100
            logger.info(
                "GG→%s opportunity: %s  GG=%.2f  %s=%.2f  ROI=%.1f%%",
                sell_market,
                coll_name,
                gg_floor / NANO,
                sell_market,
                sell_floor / NANO,
                profit_pct,
            )

            # Get actual on-sale items to find the cheapest
            coll_addr = gg._collection_addresses.get(coll_name, "")
            if not coll_addr:
                coll_addr = gg._find_collection_address(coll_name)
            if not coll_addr:
                continue

            try:
                on_sale = await gg.get_on_sale_gifts(coll_addr, limit=5)
            except Exception as e:
                logger.debug("get_on_sale_gifts error: %s", e)
                continue

            for item in on_sale:
                sale = item.get("sale", {})
                item_price = int(sale.get("fullPrice", 0) or 0)
                nft_addr = item.get("address", "")
                item_name = item.get("name", "")
                version = sale.get("version", "")

                if not nft_addr or not version or item_price <= 0:
                    continue

                if nft_addr in self._buy_cooldown:
                    continue

                if item_price >= sell_floor * (1 - min_roi):
                    continue

                max_buy = int(get_runtime("max_buy_ton") * NANO)
                if max_buy > 0 and item_price > max_buy:
                    continue

                item_profit_pct = (sell_floor - item_price) / item_price * 100
                checked += 1

                if self._shadow_mode:
                    self._stats["gg_buy_shadow"] += 1
                    self._log_event(
                        "gg_buy_shadow",
                        gift=item_name,
                        gg_price=item_price / NANO,
                        sell_floor=sell_floor / NANO,
                        sell_market=sell_market,
                        roi=round(item_profit_pct, 1),
                    )
                    await self._notify(
                        f"👻 💎 <b>Getgems→{sell_market}</b>\n\n"
                        f"🎁 {item_name}\n"
                        f"💎 Getgems: <b>{item_price / NANO:.2f}</b> TON\n"
                        f"{sell_icon} {sell_market}: <b>{sell_floor / NANO:.2f}</b> TON\n"
                        f"📈 ROI: <b>{item_profit_pct:.1f}%</b>\n\n"
                        f"⏸ Shadow mode"
                    )
                    self._buy_cooldown[nft_addr] = now
                    break

                # REAL BUY — check balance first
                balance = await gg.get_wallet_balance()
                required = item_price + 100_000_000
                if balance < required:
                    logger.warning(
                        "Insufficient balance: have %.2f, need %.2f TON",
                        balance / NANO,
                        required / NANO,
                    )
                    retry_key = f"rb:{len(self._retry_queue)}"
                    self._retry_queue[retry_key] = {
                        "nft_addr": nft_addr,
                        "version": version,
                        "item_name": item_name,
                        "item_price": item_price,
                        "sell_floor": sell_floor,
                        "sell_market": sell_market,
                    }
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🔄 Купить после пополнения",
                                    callback_data=f"retry_buy:{retry_key}",
                                )
                            ],
                        ]
                    )
                    await self._notify(
                        f"⚠️ <b>Недостаточно TON</b>\n\n"
                        f"🎁 {item_name}\n"
                        f"💎 Цена: <b>{item_price / NANO:.2f}</b> TON\n"
                        f"💰 Баланс: <b>{balance / NANO:.2f}</b> TON\n"
                        f"📈 ROI: <b>{item_profit_pct:.1f}%</b>\n\n"
                        f"Пополни кошелёк и нажми кнопку ниже:",
                        reply_markup=kb,
                    )
                    self._buy_cooldown[nft_addr] = now
                    break

                logger.info(
                    "BUYING on Getgems: %s at %.2f TON (%s floor %.2f, balance %.2f)",
                    item_name,
                    item_price / NANO,
                    sell_market,
                    sell_floor / NANO,
                    balance / NANO,
                )

                success = await gg.buy_gift(nft_addr, version)
                self._buy_cooldown[nft_addr] = now

                if success:
                    self._stats["gg_buys"] += 1
                    self._buy_prices[item_name] = item_price
                    self._log_event(
                        "gg_bought",
                        gift=item_name,
                        price=item_price / NANO,
                        sell_floor=sell_floor / NANO,
                        sell_market=sell_market,
                    )

                    self._mrkt_pending[item_name] = {
                        "buy_price": item_price,
                        "sell_floor": sell_floor,
                        "sell_market": sell_market,
                        "bought_at": now,
                        "nft_address": nft_addr,
                    }

                    await self._notify(
                        f"🛒 💎 <b>Куплено на Getgems!</b>\n\n"
                        f"🎁 {item_name}\n"
                        f"💎 Куплено за: <b>{item_price / NANO:.2f}</b> TON\n"
                        f"{sell_icon} {sell_market} floor: <b>{sell_floor / NANO:.2f}</b> TON\n"
                        f"📈 Потенциал: <b>{item_profit_pct:.1f}%</b>\n\n"
                        f"➡️ Продать на {sell_market}"
                    )
                else:
                    self._log_event(
                        "gg_buy_failed",
                        gift=item_name,
                        price=item_price / NANO,
                    )
                    await self._notify(
                        f"❌ <b>Не удалось купить</b>\n\n"
                        f"🎁 {item_name}\n"
                        f"💎 {item_price / NANO:.2f} TON\n"
                        f"Ошибка при покупке на Getgems"
                    )
                break  # one buy per collection per cycle

            await asyncio.sleep(2)  # rate limit between collections

        if checked > 0:
            logger.info("Getgems buy scan: checked %d items", checked)

    async def _check_fragment_opportunities(self) -> None:
        """Scan Fragment floors vs MRKT/Getgems for arbitrage opportunities.

        Attempts auto-buy on Fragment when profitable (if not shadow mode).
        Otherwise sends Telegram notifications.
        """
        if not self.is_market_buy_enabled("fragment"):
            return
        if not self._mds:
            return

        # Use Fragment floors from MDS cache; trigger refresh if empty
        frag_floors = self._mds.get_all_fragment_floors()
        if not frag_floors:
            # Try to load fragment floors using available collection names
            gg_names = list(self._mds.get_all_gg_floors().keys())
            if gg_names and self._fragment and self._fragment.ready:
                logger.info(
                    "Fragment scan: triggering initial floor fetch for %d collections",
                    len(gg_names[:30]),
                )
                try:
                    fetched = await self._fragment.get_floor_prices_bulk(gg_names[:30])
                    if fetched:
                        frag_floors = fetched
                except Exception as e:
                    logger.error("Fragment scan: initial fetch error: %s", e)
        if not frag_floors:
            return

        # Use bulk MRKT floors (1 API call for all 125 collections)
        try:
            mrkt_floors = await self._mds.refresh_mrkt_floors_bulk()
        except Exception as e:
            logger.debug("Fragment scan: MRKT floors refresh failed: %s", e)
            mrkt_floors = self._mds.get_all_mrkt_floors()
        gg_floors = self._mds.get_all_gg_floors() if hasattr(self._mds, "get_all_gg_floors") else {}
        portal_floors = self._mds.get_all_portal_floors()

        from bot.config import settings as _cfg
        from bot.interface.telegram_bot import get_runtime

        min_roi = get_runtime("cross_roi_pct") / 100
        max_buy = get_runtime("max_buy_ton")
        mrkt_wfee = int(_cfg.mrkt_withdraw_fee_ton * NANO)
        gg_wfee = int(_cfg.getgems_withdraw_fee_ton * NANO)

        found = 0
        logger.info(
            "Fragment scan: %d frag, %d mrkt, %d gg, %d portal floors, min_roi=%.1f%%",
            len(frag_floors),
            len(mrkt_floors),
            len(gg_floors),
            len(portal_floors),
            min_roi * 100,
        )
        for coll_name, frag_floor in frag_floors.items():
            if frag_floor <= 0:
                continue
            # Skip numeric-only names — these are gift IDs, not real collection names
            if coll_name.isdigit():
                continue

            # Fragment → MRKT: buy cheap on Fragment, sell on MRKT
            mrkt_sell = self._get_mrkt_sell_price(coll_name)
            if mrkt_sell <= 0:
                mrkt_sell = mrkt_floors.get(
                    _normalize_name(coll_name), 0,
                )
            net_mrkt = int(
                mrkt_sell * (1 - _cfg.mrkt_sell_fee_pct / 100),
            ) - mrkt_wfee
            if (net_mrkt > 0 and self.is_market_sell_enabled("mrkt")
                    and frag_floor < net_mrkt * (1 - min_roi)):
                profit_pct = (net_mrkt - frag_floor) / frag_floor * 100
                found += 1
                frag_price_ton = frag_floor / NANO

                if self._shadow_mode or frag_price_ton > max_buy:
                    self._stats["frag_buy_shadow"] += 1
                    await self._notify(
                        f"{'👻 ' if self._shadow_mode else ''}"
                        f"🔮 <b>Fragment→MRKT</b>\n\n"
                        f"🎁 {coll_name}\n"
                        f"💜 Fragment: <b>{frag_price_ton:.2f}</b> TON\n"
                        f"🟦 MRKT: <b>{mrkt_sell / NANO:.2f}</b> TON\n"
                        f"📈 ROI: <b>{profit_pct:.1f}%</b>\n\n"
                        f"{'⏸ Shadow mode' if self._shadow_mode else '⚠️ Превышает лимит'}"
                    )
                else:
                    await self._try_fragment_buy(
                        coll_name, frag_price_ton, "MRKT", mrkt_sell, profit_pct
                    )

            # Fragment → Getgems: buy cheap on Fragment, sell on Getgems
            gg_floor = gg_floors.get(coll_name, 0) or gg_floors.get(
                _normalize_name(coll_name), 0
            )
            net_gg = gg_floor - gg_wfee  # net after withdrawal fee
            if (net_gg > 0 and self.is_market_sell_enabled("getgems")
                    and frag_floor < net_gg * (1 - min_roi)):
                profit_pct = (net_gg - frag_floor) / frag_floor * 100
                found += 1
                frag_price_ton = frag_floor / NANO

                if self._shadow_mode or frag_price_ton > max_buy:
                    self._stats["frag_buy_shadow"] += 1
                    await self._notify(
                        f"{'👻 ' if self._shadow_mode else ''}"
                        f"🔮 <b>Fragment→Getgems</b>\n\n"
                        f"🎁 {coll_name}\n"
                        f"💜 Fragment: <b>{frag_price_ton:.2f}</b> TON\n"
                        f"💎 Getgems: <b>{gg_floor / NANO:.2f}</b> TON\n"
                        f"📈 ROI: <b>{profit_pct:.1f}%</b>\n\n"
                        f"{'⏸ Shadow mode' if self._shadow_mode else '⚠️ Превышает лимит'}"
                    )
                else:
                    await self._try_fragment_buy(
                        coll_name, frag_price_ton, "Getgems", gg_floor, profit_pct
                    )

            # Fragment → Portal: buy cheap on Fragment, sell on Portal (0% commission)
            portal_floor = portal_floors.get(coll_name, 0) or portal_floors.get(
                _normalize_name(coll_name), 0
            )
            if (portal_floor > 0 and self.is_market_sell_enabled("portal")
                    and frag_floor < portal_floor * (1 - min_roi)):
                profit_pct = (portal_floor - frag_floor) / frag_floor * 100
                found += 1
                frag_price_ton = frag_floor / NANO

                if self._shadow_mode or frag_price_ton > max_buy:
                    self._stats["frag_buy_shadow"] += 1
                    await self._notify(
                        f"{'👻 ' if self._shadow_mode else ''}"
                        f"🔮 <b>Fragment→Portal</b>\n\n"
                        f"🎁 {coll_name}\n"
                        f"💜 Fragment: <b>{frag_price_ton:.2f}</b> TON\n"
                        f"🟣 Portal: <b>{portal_floor / NANO:.2f}</b> TON\n"
                        f"📈 ROI: <b>{profit_pct:.1f}%</b>\n\n"
                        f"{'⏸ Shadow mode' if self._shadow_mode else '⚠️ Превышает лимит'}"
                    )
                else:
                    await self._try_fragment_buy(
                        coll_name, frag_price_ton, "Portal", portal_floor, profit_pct
                    )

        if found > 0:
            logger.info("Fragment scan: %d opportunities found", found)

    async def _try_fragment_buy(
        self,
        coll_name: str,
        price_ton: float,
        sell_market: str,
        sell_floor: int,
        roi_pct: float,
    ) -> None:
        """Attempt to buy the cheapest gift on Fragment for a collection."""
        if not self._fragment or not self._fragment.ready:
            await self._notify(
                f"🔮 <b>Fragment→{sell_market}</b>\n"
                f"🎁 {coll_name} — ROI {roi_pct:.1f}%\n"
                f"⚠️ Fragment клиент не активен"
            )
            return

        try:
            items = await self._fragment.search_on_sale(coll_name, limit=1)
            if not items:
                return

            gift = items[0]
            slug = gift.get("slug", "")
            actual_price = gift.get("price_ton", 0)
            if not slug or actual_price <= 0:
                return

            # Verify price hasn't changed
            if actual_price > price_ton * 1.05:
                logger.info(
                    "Fragment price changed for %s: %.2f → %.2f",
                    coll_name, price_ton, actual_price,
                )
                return

            # Balance pre-check (TON wallet)
            gg = self._mds._gg if hasattr(self._mds, "_gg") else None
            if gg:
                try:
                    balance = await gg.get_wallet_balance()
                    required = int(actual_price * NANO) + 100_000_000
                    if balance < required:
                        logger.warning(
                            "Fragment: insufficient balance: %.2f < %.2f",
                            balance / NANO, required / NANO,
                        )
                        await self._notify(
                            f"⚠️ <b>Fragment: недостаточно TON</b>\n\n"
                            f"🎁 {coll_name} — {actual_price:.2f} TON\n"
                            f"💰 Баланс: {balance / NANO:.2f} TON"
                        )
                        return
                except Exception as e:
                    logger.debug("Fragment balance check error: %s", e)

            clean_slug = (
                slug.removeprefix("gift/") if slug.startswith("gift/")
                else slug
            )

            result = await self._fragment.buy_gift(
                clean_slug, price_ton=actual_price,
            )
            if result and result.get("ok"):
                self._stats["frag_buys"] += 1
                self._buy_prices[coll_name] = int(actual_price * NANO)
                self._log_event("frag_buy", gift=coll_name, price=actual_price, sell_on=sell_market)

                # Auto-send NFT from Fragment to Telegram for resale
                target_user = ""
                gt = self._gift_transfer
                if gt:
                    target_user = gt.get_target_for_market(sell_market.lower()) or ""
                send_ok = False
                if sell_market in ("MRKT", "Getgems"):
                    send_result = await self._fragment.send_to_telegram(
                        clean_slug,
                        "@Hfajagsfs",
                    )
                    send_ok = bool(send_result and send_result.get("ok"))

                # Track pending transfer for reminder loop
                self._pending_transfers[clean_slug] = {
                    "name": coll_name,
                    "sell_market": sell_market,
                    "target": target_user,
                    "buy_price": actual_price,
                    "sell_price": sell_floor / NANO,
                    "roi": roi_pct,
                    "bought_at": __import__("time").time(),
                    "in_telegram": send_ok,
                }

                if send_ok:
                    await self._notify(
                        f"🔔🔔🔔 <b>ПЕРЕВЕДИ ПОДАРОК!</b> 🔔🔔🔔\n\n"
                        f"🎁 <b>{coll_name}</b>\n"
                        f"💜 Куплено на Fragment: <b>{actual_price:.2f}</b> TON\n"
                        f"📤 Уже выведен в Telegram!\n\n"
                        f"➡️ <b>Переведи на @{target_user}</b> ({sell_market})\n"
                        f"💰 Продать за: <b>{sell_floor / NANO:.2f}</b> TON\n"
                        f"📊 ROI: <b>{roi_pct:.1f}%</b>"
                    )
                else:
                    await self._notify(
                        f"🔔🔔🔔 <b>КУПЛЕНО НА FRAGMENT!</b> 🔔🔔🔔\n\n"
                        f"🎁 <b>{coll_name}</b>\n"
                        f"💜 Цена: <b>{actual_price:.2f}</b> TON\n"
                        f"⚠️ Не удалось вывести в TG автоматически\n\n"
                        f"➡️ <b>Выведи вручную и переведи на @{target_user}</b> ({sell_market})\n"
                        f"💰 Продать за: <b>{sell_floor / NANO:.2f}</b> TON\n"
                        f"📊 ROI: <b>{roi_pct:.1f}%</b>"
                    )
            else:
                await self._notify(
                    f"🔮 <b>Fragment→{sell_market}</b>\n"
                    f"🎁 {coll_name} — {actual_price:.2f} TON\n"
                    f"❌ Покупка не удалась"
                )
        except Exception as e:
            logger.error("Fragment buy attempt for %s: %s", coll_name, e)

    async def _check_portal_buys(self) -> None:
        """Scan Portal for cheap NFTs to buy and resell on MRKT/Getgems.

        Portal has 0% buy/sell fees, so any price difference is pure profit
        minus 0.25 TON withdrawal fee.
        """
        if not self.is_market_buy_enabled("portal"):
            return

        portal = self._mds._portal if hasattr(self._mds, "_portal") else None
        if not portal or not portal.authenticated:
            return

        from bot.config import settings as _cfg
        from bot.interface.telegram_bot import get_runtime

        min_roi = get_runtime("min_roi_pct") / 100
        now = time.time()

        # Sell fees per market (fraction)
        sell_fees = {
            "MRKT": _cfg.mrkt_sell_fee_pct / 100,
            "Getgems": _cfg.getgems_sell_fee_pct / 100,
        }

        # Clean expired cooldowns
        expired = [k for k, v in self._buy_cooldown.items() if now - v > 600]
        for k in expired:
            self._buy_cooldown.pop(k, None)

        # Get Portal floors and compare with MRKT/Getgems
        portal_floors = self._mds.get_all_portal_floors()
        gg_floors = (
            self._mds.get_all_gg_floors()
            if hasattr(self._mds, "get_all_gg_floors") else {}
        )

        # (name, portal_floor, net_sell, sell_market)
        candidates: list[tuple[str, int, int, str]] = []

        for coll_name, portal_floor in portal_floors.items():
            if portal_floor <= 0:
                continue

            # Check MRKT as sell target — deduct sell fee
            mrkt_sell = self._get_mrkt_sell_price(coll_name)
            mrkt_net = int(mrkt_sell * (1 - sell_fees["MRKT"]))
            if (mrkt_net > 0 and self.is_market_sell_enabled("mrkt")
                    and portal_floor < mrkt_net * (1 - min_roi)):
                candidates.append(
                    (coll_name, portal_floor, mrkt_net, "MRKT"),
                )
                continue

            # Check Getgems as sell target — deduct sell fee
            gg_floor = gg_floors.get(coll_name, 0)
            gg_net = int(gg_floor * (1 - sell_fees["Getgems"]))
            if (gg_net > 0 and self.is_market_sell_enabled("getgems")
                    and portal_floor < gg_net * (1 - min_roi)):
                candidates.append(
                    (coll_name, portal_floor, gg_net, "Getgems"),
                )

        logger.info(
            "Portal buy scan: %d Portal floors, %d candidates (ROI>%.0f%%)",
            len(portal_floors),
            len(candidates),
            min_roi * 100,
        )
        if not candidates:
            return

        candidates.sort(key=lambda x: x[2] / x[1] if x[1] > 0 else 0, reverse=True)

        for coll_name, portal_floor, sell_floor, sell_market in candidates[:3]:
            # Get actual cheapest listing on Portal
            listing = await portal.get_floor_listing(coll_name)
            if not listing:
                continue

            nft_id = listing.get("id", "")
            item_name = listing.get("name", coll_name)
            try:
                item_price_ton = float(listing.get("price", "0"))
                item_price = int(item_price_ton * NANO)
            except (ValueError, TypeError):
                continue

            if not nft_id or item_price <= 0:
                continue

            if nft_id in self._buy_cooldown:
                continue

            # Verify profitability with actual price
            profit_pct = (sell_floor - item_price) / item_price * 100 if item_price > 0 else 0
            if profit_pct < min_roi * 100:
                continue

            max_buy = int(get_runtime("max_buy_ton") * NANO)
            if max_buy > 0 and item_price > max_buy:
                continue

            if self._shadow_mode:
                self._stats["portal_buy_shadow"] += 1
                self._log_event(
                    "portal_buy_shadow",
                    gift=item_name,
                    portal_price=item_price / NANO,
                    sell_floor=sell_floor / NANO,
                    sell_market=sell_market,
                    roi=round(profit_pct, 1),
                )
                await self._notify(
                    f"👻 🟣 <b>Portal→{sell_market}</b>\n\n"
                    f"🎁 {item_name}\n"
                    f"🟣 Portal: <b>{item_price / NANO:.2f}</b> TON\n"
                    f"{'🟦' if sell_market == 'MRKT' else '💎'} {sell_market} нетто: "
                    f"<b>{sell_floor / NANO:.2f}</b> TON\n"
                    f"📈 ROI (с комиссией): <b>{profit_pct:.1f}%</b>\n\n"
                    f"⏸ Shadow mode"
                )
                self._buy_cooldown[nft_id] = now
                continue

            # REAL BUY
            balance = await portal.get_balance()
            required_ton = item_price_ton + 0.5  # buffer for fees
            if balance < required_ton:
                logger.warning(
                    "Portal: insufficient balance: have %.2f, need %.2f TON",
                    balance,
                    required_ton,
                )
                await self._notify(
                    f"⚠️ <b>Portal: недостаточно баланса</b>\n\n"
                    f"🎁 {item_name} — {item_price / NANO:.2f} TON\n"
                    f"💰 Баланс: {balance:.2f} TON\n"
                    f"📈 ROI: {profit_pct:.1f}%"
                )
                self._buy_cooldown[nft_id] = now
                continue

            logger.info(
                "BUYING on Portal: %s at %.2f TON (sell on %s at %.2f, balance %.2f)",
                item_name,
                item_price / NANO,
                sell_market,
                sell_floor / NANO,
                balance,
            )

            success = await portal.buy_single(nft_id, listing.get("price", str(item_price_ton)))
            self._buy_cooldown[nft_id] = now

            if success:
                self._stats["portal_buys"] += 1
                self._buy_prices[item_name] = item_price
                self._cross_market_targets[item_name] = sell_market
                self._log_event(
                    "portal_bought",
                    gift=item_name,
                    price=item_price / NANO,
                    sell_market=sell_market,
                    sell_floor=sell_floor / NANO,
                )
                await self._notify(
                    f"🛒 🟣 <b>Куплено на Portal!</b>\n\n"
                    f"🎁 {item_name}\n"
                    f"🟣 Куплено за: <b>{item_price / NANO:.2f}</b> TON\n"
                    f"{'🟦' if sell_market == 'MRKT' else '💎'} {sell_market} нетто: "
                    f"<b>{sell_floor / NANO:.2f}</b> TON\n"
                    f"📈 ROI (с комиссией): <b>{profit_pct:.1f}%</b>\n\n"
                    f"➡️ Withdraw → transfer → sell on {sell_market}"
                )
            else:
                self._log_event(
                    "portal_buy_failed",
                    gift=item_name,
                    price=item_price / NANO,
                )
                await self._notify(
                    f"❌ 🟣 Portal: не удалось купить {item_name}"
                )

            await asyncio.sleep(2)

    async def _check_portal_sells(self) -> None:
        """Check Portal inventory and list unlisted NFTs for sale."""
        if not self.is_market_sell_enabled("portal"):
            return

        portal = self._mds._portal if hasattr(self._mds, "_portal") else None
        if not portal or not portal.authenticated:
            logger.info("Portal sells: skip — not authenticated")
            return

        try:
            owned = await portal.get_owned_nfts(limit=50, status="unlisted")
        except Exception as e:
            logger.info("Portal get_owned_nfts failed: %s", e)
            return

        logger.info("Portal sells: %d unlisted NFTs found", len(owned) if owned else 0)
        if not owned:
            return

        portal_floors = self._mds.get_all_portal_floors()

        for nft in owned:
            nft_id = nft.get("id", "")
            nft_name = nft.get("name", "")
            coll_id = nft.get("collection_id", "")
            coll_name = portal.get_collection_name(coll_id)

            if not nft_id or not coll_name:
                continue

            # Skip if this NFT was bought for cross-market sale (e.g. Portal→Getgems)
            target_market = self._cross_market_targets.get(coll_name)
            if target_market and target_market.lower() != "portal":
                logger.info(
                    "Portal sells: skip %s — cross-market target is %s",
                    nft_name, target_market,
                )
                continue

            # Find best sell price across markets
            portal_floor = portal_floors.get(coll_name, 0)
            if portal_floor <= 0:
                continue

            # List at Portal floor (undercut by 1%)
            list_price_nano = int(portal_floor * 0.99)
            # Never list below buy price (prevent losses)
            buy_price_min = self._buy_prices.get(coll_name, 0)
            if buy_price_min > 0 and list_price_nano < buy_price_min:
                list_price_nano = buy_price_min
                logger.info(
                    "Portal: raised price to buy_price %.2f for %s",
                    list_price_nano / NANO, nft_name,
                )
            list_price_ton = list_price_nano / NANO

            if self._shadow_mode:
                logger.info(
                    "Portal shadow list: %s at %.2f TON", nft_name, list_price_ton
                )
                continue

            success = await portal.list_single(nft_id, list_price_ton)
            if success:
                self._stats["portal_sells"] += 1
                self._log_event("portal_listed", gift=nft_name, price=list_price_ton)
                await self._notify(
                    f"📋 🟣 <b>Выставлено на Portal</b>\n\n"
                    f"🎁 {nft_name}\n"
                    f"💰 Цена: <b>{list_price_ton:.2f}</b> TON"
                )

    async def _remind_pending_transfers(self) -> None:
        """Remind user about gifts that need manual transfer."""
        if not self._pending_transfers:
            return

        import time as _time

        now = _time.time()
        done = []

        for slug, info in self._pending_transfers.items():
            age_min = (now - info["bought_at"]) / 60
            target = info.get("target", "?")
            market = info.get("sell_market", "?")
            name = info.get("name", slug)

            # Check if gift was already transferred via Telethon
            gt = self._gift_transfer
            if gt and gt.ready:
                gifts = await gt.get_my_gifts(limit=50)
                slugs_in_tg = {g.get("slug", "").lower() for g in gifts if g.get("is_unique")}
                if slug.lower() not in slugs_in_tg:
                    done.append(slug)
                    continue

            await self._notify(
                f"🔔🔔🔔 <b>ПЕРЕВЕДИ ПОДАРОК!</b> 🔔🔔🔔\n\n"
                f"🎁 <b>{name}</b>\n"
                f"➡️ Переведи на <b>@{target}</b> ({market})\n"
                f"💰 Продать за: <b>{info.get('sell_price', 0):.2f}</b> TON\n"
                f"📊 ROI: <b>{info.get('roi', 0):.1f}%</b>\n"
                f"⏰ Куплен {age_min:.0f} мин назад"
            )

        for s in done:
            self._pending_transfers.pop(s, None)
            logger.info("Pending transfer resolved: %s", s)

    async def run(self) -> None:
        """Background loop with timeout protection per cycle."""
        self._running = True
        logger.info("AutoSeller started (interval=%.0fs)", self._check_interval)

        cycle = 0
        reminder_counter = 0
        while self._running:
            try:
                await asyncio.wait_for(
                    self._run_cycle(cycle, reminder_counter),
                    timeout=90.0,
                )
                reminder_counter += 1
                if reminder_counter >= 3:
                    reminder_counter = 0
            except asyncio.TimeoutError:
                logger.warning("AutoSeller cycle timeout (>90s)")
            except Exception:
                logger.exception("AutoSeller error")

            # Fragment scan: separate timeout, every 3rd cycle
            if cycle % 3 == 0:
                try:
                    await asyncio.wait_for(
                        self._check_fragment_opportunities(),
                        timeout=300.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Fragment scan timeout (>300s)")
                except Exception:
                    logger.exception("Fragment scan error")

            cycle += 1
            await asyncio.sleep(self._check_interval)

    async def _run_cycle(
        self, cycle: int, reminder_counter: int,
    ) -> None:
        if self._enabled:
            await self.check_and_list()
            await self._check_getgems_buys()
            await self._check_portal_buys()
        await self._check_getgems_pending()
        await self._auto_list_unlisted_getgems()
        await self._check_portal_sells()
        if reminder_counter >= 2:
            await self._remind_pending_transfers()

    def dismiss_transfer(self, slug: str) -> bool:
        """Mark a pending transfer as done (stop reminders)."""
        if slug in self._pending_transfers:
            self._pending_transfers.pop(slug)
            return True
        return False

    def stop(self) -> None:
        self._running = False

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._running,
            "shadow_mode": self._shadow_mode,
            "markets_enabled": {
                m: {
                    "buy": self._markets_buy_enabled.get(m, True),
                    "sell": self._markets_sell_enabled.get(m, True),
                }
                for m in ("mrkt", "getgems", "fragment", "portal")
            },
            "stats": dict(self._stats),
            "log": list(self._log)[-10:],
            "gg_pending": {
                k: {
                    "price": v["price"] / NANO,
                    "number": v.get("number", ""),
                    "age_min": int((time.time() - v["withdrawn_at"]) / 60),
                }
                for k, v in self._gg_pending.items()
            },
            "mrkt_pending": {
                k: {
                    "buy_price": v["buy_price"] / NANO,
                    "mrkt_floor": v["mrkt_floor"] / NANO,
                    "age_min": int((time.time() - v["bought_at"]) / 60),
                }
                for k, v in self._mrkt_pending.items()
            },
            "pending_transfers": {
                k: {
                    "name": v["name"],
                    "target": f"@{v['target']}",
                    "market": v["sell_market"],
                    "roi": f"{v['roi']:.1f}%",
                }
                for k, v in self._pending_transfers.items()
            },
        }
