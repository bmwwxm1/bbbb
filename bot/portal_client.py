"""Portal Market (portal-market.com) client — Telegram Mini App marketplace.

Portal is a custodial marketplace for Telegram Gifts:
- 0% commission on buy/sell (only 0.25 TON withdrawal fee)
- REST API: collections, listings, buy, sell, withdraw
- Auth: Telegram Mini App (TMA) init data in Authorization header
- Import via @GiftsToPortals bot, export via /nfts/withdraw

Public endpoints (no auth): collections, listings, search, market config
Private endpoints (TMA auth): buy, sell, list, unlist, offers, wallet, withdraw
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

NANO = 1_000_000_000
API_BASE = "https://portal-market.com/api"

# Rate limiting
_MIN_REQUEST_INTERVAL = 0.3  # seconds between requests


class PortalClient:
    """Client for Portal Market (portal-market.com)."""

    def __init__(self, tma_init_data: str = "") -> None:
        self._tma_init_data = tma_init_data
        self._session: aiohttp.ClientSession | None = None
        self._last_request_time: float = 0.0
        self._ready = False
        self._auth_alert_sent = False
        self._session_dirty = False
        self._on_auth_expired: Callable[[], Coroutine[Any, Any, None]] | None = None

        # Collection cache: name -> uuid
        self._collection_ids: dict[str, str] = {}
        # UUID -> name reverse map
        self._id_to_name: dict[str, str] = {}
        # Cached floors: name -> price in TON (float string from API)
        self._cached_floors: dict[str, float] = {}
        self._last_collections_fetch: float = 0.0

    @property
    def ready(self) -> bool:
        return bool(self._tma_init_data)

    @property
    def authenticated(self) -> bool:
        return bool(self._tma_init_data)

    def set_auth_expired_callback(
        self, cb: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        self._on_auth_expired = cb

    def update_auth(self, tma_init_data: str) -> None:
        """Update TMA init data at runtime without restart."""
        self._tma_init_data = tma_init_data
        self._auth_alert_sent = False
        self._session_dirty = True
        logger.info("Portal TMA auth updated")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── HTTP ──────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_dirty and self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self._session_dirty = False
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko)"
                ),
                "Referer": "https://portal-market.com/",
            }
            if self._tma_init_data:
                headers["Authorization"] = f"tma {self._tma_init_data}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def _reset_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        if require_auth and not self._tma_init_data:
            logger.warning("Portal: auth required but no TMA init data")
            return None

        await self._throttle()
        session = await self._get_session()
        url = f"{API_BASE}{path}"

        try:
            async with session.request(
                method, url, params=params, json=json_data, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 401:
                    logger.warning("Portal: auth expired (401)")
                    old_data = self._tma_init_data
                    if self._on_auth_expired:
                        try:
                            await self._on_auth_expired()
                        except Exception:
                            pass
                    # Wait up to 45s for auth refresh (Telegram API call takes time)
                    for _ in range(9):
                        if self._tma_init_data != old_data:
                            break
                        await asyncio.sleep(5)
                    if self._tma_init_data != old_data:
                        await self._throttle()
                        session = await self._get_session()
                        async with session.request(
                            method, url, params=params, json=json_data,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as retry_resp:
                            if retry_resp.status < 400:
                                return await retry_resp.json()
                            logger.warning(
                                "Portal: retry after auth refresh → %d",
                                retry_resp.status,
                            )
                    return None
                if resp.status == 429:
                    logger.warning("Portal: rate limited (429)")
                    await asyncio.sleep(5)
                    return None
                if resp.status >= 400:
                    text = await resp.text()
                    logger.error("Portal %s %s → %d: %s", method, path, resp.status, text[:300])
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            logger.warning("Portal timeout: %s %s", method, path)
            return None
        except Exception as e:
            logger.error("Portal request error: %s %s: %s", method, path, e)
            return None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(
        self, path: str, data: dict[str, Any] | None = None, require_auth: bool = True
    ) -> Any:
        return await self._request("POST", path, json_data=data, require_auth=require_auth)

    # ── Collections ───────────────────────────────────────────────────

    async def get_collections(self) -> list[dict[str, Any]]:
        """Get all collections with floor prices, volume, listed count.

        No auth needed. Returns up to 200 collections.
        """
        data = await self._get("/collections", params={"limit": "200"})
        if not data or not isinstance(data, dict):
            return []
        collections = data.get("collections", [])

        # Cache id -> name mapping
        for c in collections:
            name = c.get("name", "")
            cid = c.get("id", "")
            if name and cid:
                self._collection_ids[name] = cid
                self._id_to_name[cid] = name
                floor_str = c.get("floor_price", "0")
                try:
                    self._cached_floors[name] = float(floor_str)
                except (ValueError, TypeError):
                    pass

        self._last_collections_fetch = time.monotonic()
        return collections

    async def get_collection_floors_bulk(self) -> dict[str, int]:
        """Get all collection floors in 1 request. Returns {name: nanoTON}.

        NOTE: Portal's floor_price from /collections is approximate — often
        5-20% below the actual cheapest listing. Use get_floor_listing() or
        get_collection_floor() for real listing prices before buying.
        """
        collections = await self.get_collections()
        floors: dict[str, int] = {}
        for c in collections:
            name = c.get("name", "")
            floor_str = c.get("floor_price", "0")
            try:
                floor_ton = float(floor_str)
                if floor_ton > 0 and name:
                    floors[name] = int(floor_ton * NANO)
            except (ValueError, TypeError):
                continue
        return floors

    def get_collection_id(self, name: str) -> str:
        """Get Portal UUID for a collection name."""
        return self._collection_ids.get(name, "")

    def get_collection_name(self, cid: str) -> str:
        """Get collection name from Portal UUID."""
        return self._id_to_name.get(cid, "")

    # ── Listings / Search ─────────────────────────────────────────────

    async def search_listings(
        self,
        collection_id: str,
        sort: str = "price_asc",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search NFT listings for a collection. No auth needed.

        Returns list of NFT objects with id, name, price, attributes, etc.
        """
        data = await self._get(
            "/nfts/search",
            params={
                "collection_id": collection_id,
                "sort": sort,
                "limit": str(limit),
            },
        )
        if not data or not isinstance(data, dict):
            return []
        return data.get("results", [])

    async def get_floor_listing(self, collection_name: str) -> dict[str, Any] | None:
        """Get cheapest listing for a collection."""
        cid = self.get_collection_id(collection_name)
        if not cid:
            return None
        listings = await self.search_listings(cid, sort="price_asc", limit=1)
        return listings[0] if listings else None

    async def get_collection_floor(self, collection_name: str) -> int:
        """Get floor price for a collection in nanoTON."""
        cid = self.get_collection_id(collection_name)
        if not cid:
            return 0
        listings = await self.search_listings(cid, sort="price_asc", limit=1)
        if not listings:
            return 0
        try:
            return int(float(listings[0].get("price", "0")) * NANO)
        except (ValueError, TypeError):
            return 0

    # ── Market Config ─────────────────────────────────────────────────

    async def get_market_config(self) -> dict[str, Any]:
        """Get market config: commission, fees, wallet, rates. No auth needed."""
        data = await self._get("/market/config")
        return data if isinstance(data, dict) else {}

    async def get_market_actions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get recent market activity feed. No auth needed."""
        data = await self._get("/market/actions/")
        if not data or not isinstance(data, dict):
            return []
        return data.get("actions", [])[:limit]

    # ── Buy ───────────────────────────────────────────────────────────

    async def check_availability(
        self, nft_details: list[dict[str, str]]
    ) -> dict[str, Any] | None:
        """Check if NFTs are still available for purchase.

        Args:
            nft_details: list of {"id": uuid, "price": "2.49"}
        """
        return await self._post(
            "/nfts/check-availability",
            data={"nft_details": nft_details},
            require_auth=True,
        )

    async def buy_nfts(
        self, nft_details: list[dict[str, str]]
    ) -> dict[str, Any] | None:
        """Buy NFTs on Portal.

        Args:
            nft_details: list of {"id": uuid, "price": "2.49"}

        Returns purchase result with purchase_results list.
        """
        if not nft_details:
            return None
        return await self._post(
            "/nfts", data={"nft_details": nft_details}, require_auth=True
        )

    async def buy_single(self, nft_id: str, price: str) -> bool:
        """Buy a single NFT. Returns True on success."""
        result = await self.buy_nfts([{"id": nft_id, "price": price}])
        if result is None:
            return False
        total_purchased = result.get("total_purchased", 0)
        return total_purchased > 0

    # ── Sell / List ───────────────────────────────────────────────────

    async def bulk_list(self, items: list[dict[str, str]]) -> dict[str, Any] | None:
        """List multiple NFTs for sale.

        items: [{"nft_id": "uuid", "price": "10.5"}, ...]
        """
        return await self._post(
            "/nfts/bulk-list",
            data={"nft_prices": items},
            require_auth=True,
        )

    async def list_single(self, nft_id: str, price_ton: float) -> bool:
        """List a single NFT for sale at specified price (TON)."""
        price_str = f"{price_ton:.2f}"
        result = await self.bulk_list([{"nft_id": nft_id, "price": price_str}])
        return result is not None

    async def bulk_unlist(self, nft_ids: list[str]) -> dict[str, Any] | None:
        """Remove NFTs from sale."""
        return await self._post("/nfts/bulk-unlist", data={"nft_ids": nft_ids}, require_auth=True)

    async def unlist_single(self, nft_id: str) -> bool:
        """Remove a single NFT from sale."""
        result = await self.bulk_unlist([nft_id])
        return result is not None

    # ── Quick Sale (instant sell into buy orders) ─────────────────────

    async def quick_sale_preview(self, nft_ids: list[str]) -> dict[str, Any] | None:
        """Preview quick sale — shows how much you'd get selling into buy orders."""
        return await self._post(
            "/nfts/quick-sale/preview",
            data={"nft_ids": nft_ids},
            require_auth=True,
        )

    async def quick_sale_execute(self, nft_ids: list[str]) -> dict[str, Any] | None:
        """Execute quick sale — instantly sell into existing buy orders."""
        return await self._post(
            "/nfts/quick-sale",
            data={"nft_ids": nft_ids},
            require_auth=True,
        )

    # ── Offers (collection buy orders) ────────────────────────────────

    async def get_collection_offers_top(self, collection_id: str) -> list[dict[str, Any]]:
        """Get top buy orders for a collection. Auth required."""
        data = await self._request(
            "GET",
            f"/collection-offers/{collection_id}/top",
            require_auth=True,
        )
        if not data or not isinstance(data, dict):
            return []
        return data.get("offers", [])

    async def get_best_offer(self, collection_name: str) -> int:
        """Get best (highest) buy offer for a collection in nanoTON."""
        cid = self.get_collection_id(collection_name)
        if not cid:
            return 0
        offers = await self.get_collection_offers_top(cid)
        if not offers:
            return 0
        try:
            return int(float(offers[0].get("amount", "0")) * NANO)
        except (ValueError, TypeError):
            return 0

    async def get_placed_offers(self) -> list[dict[str, Any]]:
        """Get offers placed by current user."""
        data = await self._request("GET", "/offers/placed", require_auth=True)
        if not data or not isinstance(data, dict):
            return []
        return data.get("offers", [])

    # ── Inventory ─────────────────────────────────────────────────────

    async def get_owned_nfts(
        self,
        limit: int = 100,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get NFTs owned by current user.

        status: 'listed', 'unlisted', or None for all.
        """
        params: dict[str, str] = {"limit": str(limit)}
        if status:
            params["status"] = status
        data = await self._request("GET", "/nfts/owned", params=params, require_auth=True)
        if not data or not isinstance(data, dict):
            return []
        return data.get("nfts", [])

    async def get_inventory_cost(self) -> dict[str, Any]:
        """Get total inventory cost breakdown."""
        data = await self._request("GET", "/users/me/inventory/cost", require_auth=True)
        return data if isinstance(data, dict) else {}

    # ── Wallet ────────────────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Get Portal wallet balance in TON."""
        data = await self._request("GET", "/users/wallets/", require_auth=True)
        if not data or not isinstance(data, dict):
            return 0.0
        try:
            return float(data.get("balance", "0"))
        except (ValueError, TypeError):
            return 0.0

    async def get_wallet_limits(self) -> dict[str, Any]:
        """Get wallet transaction limits."""
        data = await self._request("GET", "/users/wallets/limits", require_auth=True)
        return data if isinstance(data, dict) else {}

    async def generate_deposit_id(self) -> str:
        """Generate a deposit ID for funding the Portal wallet.

        Send TON to market config deposit_wallet with this ID as memo.
        """
        data = await self._post("/deposits", require_auth=True)
        if not data or not isinstance(data, dict):
            return ""
        return data.get("id", "")

    # ── Withdraw ──────────────────────────────────────────────────────

    async def withdraw_nfts(self, nft_ids: list[str]) -> dict[str, Any] | None:
        """Withdraw NFTs from Portal back to Telegram.

        Costs 0.25 TON per NFT.
        """
        return await self._post(
            "/nfts/withdraw",
            data={"nft_ids": nft_ids},
            require_auth=True,
        )

    async def withdraw_single(self, nft_id: str) -> bool:
        """Withdraw a single NFT. Returns True on success."""
        result = await self.withdraw_nfts([nft_id])
        return result is not None

    async def withdraw_ton(self, amount: str, address: str) -> dict[str, Any] | None:
        """Withdraw TON to an external wallet."""
        return await self._post(
            "/users/wallets/withdraw",
            data={"amount": amount, "address": address},
            require_auth=True,
        )

    # ── Trait Floors ──────────────────────────────────────────────────

    async def get_backdrop_floors(self, collection_id: str) -> dict[str, float]:
        """Get floor prices per backdrop trait. No auth needed.

        Returns {backdrop_name: floor_ton}.
        """
        data = await self._get(
            "/collections/filters/backdrops/floors",
            params={"collection_id": collection_id},
        )
        if not data or not isinstance(data, dict):
            return {}
        raw = data.get("floorPrices", {})
        result: dict[str, float] = {}
        for name, price in raw.items():
            try:
                result[name] = float(price)
            except (ValueError, TypeError):
                continue
        return result

    # ── User Stats ────────────────────────────────────────────────────

    async def get_profile_stats(self) -> dict[str, Any]:
        """Get user trading stats."""
        data = await self._request("GET", "/users/profile/stats", require_auth=True)
        return data if isinstance(data, dict) else {}

    async def get_wallet_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get wallet transaction history."""
        data = await self._request(
            "GET",
            "/users/wallets/history",
            params={"limit": str(limit)},
            require_auth=True,
        )
        if not data or not isinstance(data, dict):
            return []
        return data.get("actions", [])
