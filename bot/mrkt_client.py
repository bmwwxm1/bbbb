import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

from bot.config import settings

logger = logging.getLogger(__name__)

NANO = 1_000_000_000
PRICE_STEP = 100_000_000  # 0.1 TON — MRKT minimum price increment


def nanoton_to_ton(nanoton: int) -> float:
    return nanoton / NANO


def ton_to_nanoton(ton: float) -> int:
    return int(ton * NANO)


def round_price(nanoton: int) -> int:
    """Round price DOWN to nearest 0.1 TON (MRKT price step)."""
    return (nanoton // PRICE_STEP) * PRICE_STEP


class MRKTClient:
    """Async client for MRKT marketplace API."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._delay = settings.request_delay_seconds
        self._max_backoff = 120.0
        self._auth_token = (settings.mrkt_auth_token or "").strip()
        self._session_dirty = False
        self._on_token_expired: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._token_alert_sent = False

    def set_token_expired_callback(self, cb: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._on_token_expired = cb

    def update_token(self, new_token: str) -> None:
        """Update MRKT auth token at runtime and reset session."""
        self._auth_token = new_token.strip()
        self._session_dirty = True
        self._token_alert_sent = False
        logger.info("MRKT auth token updated")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._auth_token,
            "Cookie": f"access_token={self._auth_token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": settings.mrkt_origin,
            "Referer": f"{settings.mrkt_origin}/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko)"
            ),
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_dirty and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._session_dirty = False
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self, method: str, path: str, json_data: dict[str, Any] | None = None
    ) -> Any:
        url = f"{settings.mrkt_api_base}{path}"

        for attempt in range(5):
            try:
                session = await self._get_session()
                async with session.request(method, url, json=json_data) as resp:
                    if resp.status == 429:
                        wait = min(self._delay * (2**attempt), 30.0)
                        logger.warning(
                            "Rate limited, backing off %.1fs (attempt %d)",
                            wait,
                            attempt,
                        )
                        await asyncio.sleep(wait)
                        continue

                    if resp.status in (401, 403):
                        text = await resp.text()
                        logger.error(
                            "Auth error %d on %s: %s",
                            resp.status,
                            path,
                            text[:200],
                        )
                        old_token = self._auth_token
                        if self._on_token_expired:
                            try:
                                await self._on_token_expired()
                            except Exception as e:
                                logger.warning("Token refresh callback error: %s", e)
                        # Retry if token was refreshed
                        if self._auth_token != old_token:
                            self._session_dirty = True
                            self._token_alert_sent = False
                            logger.info("MRKT: token refreshed, retrying %s", path)
                            continue
                        # Only alert once per token failure cycle
                        if not self._token_alert_sent:
                            self._token_alert_sent = True
                        return None

                    if resp.status >= 400:
                        text = await resp.text()
                        logger.error(
                            "API error %d on %s: %s",
                            resp.status,
                            path,
                            text[:200],
                        )
                        return None

                    body = await resp.read()
                    # Minimal delay between requests to avoid rate limits
                    await asyncio.sleep(max(self._delay, 0.3))
                    if not body:
                        return {"ok": True, "status": resp.status}
                    return json.loads(body)

            except RuntimeError as e:
                if "Session is closed" in str(e):
                    logger.warning(
                        "Session was closed, refreshing (attempt %d)",
                        attempt,
                    )
                    self._session = None
                    continue
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error("Request error on %s: %s", path, e)
                await asyncio.sleep(self._delay)

        logger.error("Max retries reached for %s", path)
        return None

    async def _get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def _post(self, path: str, data: dict[str, Any]) -> Any:
        return await self._request("POST", path, data)

    # ── Account ──────────────────────────────────────────────────────────

    async def get_me(self) -> dict[str, Any] | None:
        return await self._get("/me")

    async def get_gift_statistics(self) -> dict[str, Any] | None:
        return await self._get("/gift-statistics")

    async def get_my_gifts(
        self,
        owner_tg_id: int,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """Get gifts owned by user via /gifts with ownerTgId filter."""
        body = {
            "count": count,
            "cursor": "",
            "collectionNames": [],
            "modelNames": [],
            "backdropNames": [],
            "symbolNames": [],
            "ordering": "Price",
            "lowToHigh": True,
            "ownerTgId": owner_tg_id,
            "minPrice": None,
            "maxPrice": None,
            "number": None,
            "isPremarket": None,
            "isNew": None,
            "luckyBuy": None,
            "giftType": None,
            "craftable": None,
            "isCrafted": None,
            "tgCanBeCraftedFrom": None,
            "removeSelfSales": False,
            "isTransferable": None,
            "query": None,
        }
        data = await self._post("/gifts", body)
        if data and isinstance(data, dict):
            return data.get("gifts", [])
        return []

    # ── Balance ─────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """Get MRKT account balance. Returns {hard: nanoTON, stars, spices, ...}."""
        data = await self._get("/balance")
        if data and isinstance(data, dict):
            return data
        return {}

    # ── Collections & Reference Data ────────────────────────────────────

    async def get_collections(self) -> list[dict[str, Any]]:
        data = await self._get("/gifts/collections")
        return data if isinstance(data, list) else []

    # ── Listings (gifts on sale) ────────────────────────────────────────

    async def get_listings(
        self,
        collection_names: list[str] | None = None,
        model_names: list[str] | None = None,
        backdrop_names: list[str] | None = None,
        symbol_names: list[str] | None = None,
        count: int = 20,
        cursor: str = "",
        ordering: str = "Price",
        low_to_high: bool = True,
        min_price: int | None = None,
        max_price: int | None = None,
    ) -> dict[str, Any]:
        body = {
            "count": count,
            "cursor": cursor,
            "collectionNames": collection_names or [],
            "modelNames": model_names or [],
            "backdropNames": backdrop_names or [],
            "symbolNames": symbol_names or [],
            "minPrice": min_price,
            "maxPrice": max_price,
            "number": None,
            "isPremarket": None,
            "isNew": None,
            "luckyBuy": None,
            "giftType": None,
            "craftable": None,
            "isCrafted": None,
            "tgCanBeCraftedFrom": None,
            "removeSelfSales": True,
            "isTransferable": None,
            "ordering": ordering,
            "lowToHigh": low_to_high,
            "query": None,
        }
        data = await self._post("/gifts/saling", body)
        if data is None:
            return {"cursor": "", "gifts": []}
        return data

    async def get_gift_details(self, gift_id: str) -> dict[str, Any] | None:
        return await self._get(f"/gifts/gift/{gift_id}")

    # ── Orders (buy orders) ─────────────────────────────────────────────

    async def get_all_collection_top_orders(self) -> list[dict[str, Any]]:
        data = await self._get("/orders/all-collection-top")
        return data if isinstance(data, list) else []

    async def get_orders(
        self,
        collection_names: list[str] | None = None,
        count: int = 20,
        cursor: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "count": count,
            "cursor": cursor,
        }
        if collection_names:
            body["collectionNames"] = collection_names
        data = await self._post("/orders", body)
        if data is None:
            return {"cursor": "", "orders": []}
        return data

    async def get_orders_top(self, filters: dict[str, Any]) -> dict[str, Any] | None:
        return await self._post("/orders/top", filters)

    # ── Trading Actions ─────────────────────────────────────────────────

    async def buy_gifts(self, gift_ids: list[str], prices: dict[str, int]) -> dict[str, Any] | None:
        if not gift_ids:
            logger.warning("buy_gifts called with empty gift_ids")
            return None
        for gid, price in prices.items():
            if price <= 0:
                logger.error("buy_gifts: non-positive price %d for %s", price, gid)
                return None
        body = {"ids": gift_ids, "prices": prices}
        return await self._post("/gifts/buy", body)

    async def sell_gift(self, gift_id: str, price: int) -> dict[str, Any] | None:
        body = {"ids": [gift_id], "price": price}
        return await self._post("/gifts/sale", body)

    async def change_sale_price(self, gift_id: str, price: int) -> dict[str, Any] | None:
        body = {"id": gift_id, "price": price}
        return await self._post("/gifts/sale/change-price", body)

    async def cancel_sale(self, gift_id: str) -> dict[str, Any] | None:
        body = {"id": gift_id}
        return await self._post("/gifts/sale/cancel", body)

    async def withdraw_gift(self, gift_id: str) -> dict[str, Any] | None:
        """Withdraw a gift from MRKT back to Telegram via /gifts/return."""
        body = {"ids": [gift_id]}
        return await self._post("/gifts/return", body)

    async def fill_order(self, order_id: str, gift_ids: list[str]) -> dict[str, Any] | None:
        body = {"orderId": order_id, "giftIds": gift_ids}
        return await self._post("/orders/fill/", body)

    async def create_order(
        self,
        collection_name: str,
        price_nanoton: int,
        quantity: int = 1,
        model_name: str | None = None,
        backdrop_name: str | None = None,
        symbol_name: str | None = None,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            "collectionName": collection_name,
            "modelName": model_name,
            "backdropName": backdrop_name,
            "symbolName": symbol_name,
            "priceMinNanoTONs": price_nanoton,
            "priceMaxNanoTONs": price_nanoton,
            "quantity": quantity,
        }
        return await self._post("/orders/create", body)

    async def cancel_order(self, order_id: str) -> dict[str, Any] | None:
        return await self._post(f"/orders/cancel/{order_id}", {})

    # ── Feed (recent events) ───────────────────────────────────────────

    async def get_feed(
        self,
        count: int = 20,
        cursor: str = "",
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"count": count, "cursor": cursor}
        if filters:
            body.update(filters)
        data = await self._post("/feed", body)
        if data is None:
            return {"cursor": "", "items": []}
        return data

    # ── My gifts ────────────────────────────────────────────────────────

    async def get_my_listings(self, count: int = 20, cursor: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "count": count,
            "cursor": cursor,
        }
        data = await self._post(
            "/gifts/saling",
            {
                **body,
                "collectionNames": [],
                "modelNames": [],
                "backdropNames": [],
                "symbolNames": [],
                "minPrice": None,
                "maxPrice": None,
                "number": None,
                "isPremarket": None,
                "isNew": None,
                "luckyBuy": None,
                "giftType": None,
                "craftable": None,
                "isCrafted": None,
                "tgCanBeCraftedFrom": None,
                "removeSelfSales": False,
                "isTransferable": None,
                "ordering": "Price",
                "lowToHigh": True,
                "query": None,
            },
        )
        if data is None:
            return {"cursor": "", "gifts": []}
        return data

    async def get_my_orders(self, count: int = 20, cursor: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"count": count, "cursor": cursor}
        data = await self._post("/orders/get-my-orders", body)
        if data is None:
            return {"cursor": "", "orders": []}
        return data

    async def get_my_feed(self, count: int = 20, cursor: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "count": count,
            "cursor": cursor,
            "feedType": "MyEvents",
        }
        data = await self._post("/feed", body)
        if data is None:
            return {"cursor": "", "items": []}
        return data
