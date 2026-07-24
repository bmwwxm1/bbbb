"""Unified market data service — bridges API clients to the trading system.

Single point of entry for fetching market data from both MRKT and Getgems.
Handles:
  - Fetching raw data from API clients
  - Normalizing through data quality layer
  - Building MarketSnapshots
  - Caching for rate limiting
  - Cross-market spread detection
  - Opportunity creation
"""

from __future__ import annotations

import logging
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.data.normalizer import (
    build_snapshot,
    normalize_listings,
    normalize_orders,
    normalize_sales,
)
from bot.intelligence.cost_model import calculate_pnl
from bot.intelligence.liquidation_model import LiquidationModel
from bot.intelligence.pricing_engine import PricingEngine
from bot.models.types import (
    DEADLINE_COLD_SEC,
    DEADLINE_HOT_SEC,
    DEADLINE_WARM_SEC,
    Listing,
    Market,
    MarketSnapshot,
    Opportunity,
    Strategy,
)

logger = logging.getLogger(__name__)


def _normalize_name(name: str) -> str:
    """Normalize collection name for cross-market matching.

    Handles Unicode variants (curly vs straight apostrophes, etc.)
    so 'Khabib\u2019s Papakha' matches "Khabib's Papakha".
    """
    normalized = unicodedata.normalize("NFKD", name)
    normalized = normalized.replace("\u2019", "'").replace("\u2018", "'")
    return normalized


class MarketDataService:
    """Fetches and normalizes data from MRKT + Getgems + Fragment + Portal."""

    def __init__(
        self,
        mrkt_client: Any,
        getgems_client: Any,
        pricing_engine: PricingEngine,
        liquidation_model: LiquidationModel,
        fragment_client: Any = None,
        portal_client: Any = None,
    ) -> None:
        self._mrkt = mrkt_client
        self._gg = getgems_client
        self._fragment = fragment_client
        self._portal = portal_client
        self._pricing = pricing_engine
        self._liquidation = liquidation_model

        # Snapshot cache
        self._mrkt_snapshots: dict[str, MarketSnapshot] = {}
        self._mrkt_bulk_floors: dict[str, int] = {}
        self._last_mrkt_bulk_fetch: float = 0
        self._mrkt_bulk_fetch_interval = 30.0  # seconds between MRKT bulk fetches
        self._gg_floors: dict[str, int] = {}
        self._fragment_floors: dict[str, int] = {}
        self._portal_floors: dict[str, int] = {}
        self._last_gg_fetch: float = 0
        self._last_fragment_fetch: float = 0
        self._last_portal_fetch: float = 0
        self._gg_fetch_interval = 60.0  # seconds between Getgems bulk fetches
        self._fragment_fetch_interval = 120.0  # seconds between Fragment bulk fetches
        self._portal_fetch_interval = 30.0  # seconds between Portal bulk fetches

        # Bulk order cache
        self._mrkt_top_orders: dict[str, dict[str, Any]] = {}
        self._last_top_orders_fetch: float = 0
        self._top_orders_fetch_interval = 30.0

        # Collection name mapping (title → API name for numeric IDs)
        self._title_to_api_name: dict[str, str] = {}
        self._api_name_to_title: dict[str, str] = {}

    async def fetch_mrkt_snapshot(
        self, collection: str, market: Market = Market.MRKT
    ) -> MarketSnapshot | None:
        """Fetch fresh MRKT data for one collection.

        Sequential calls to avoid rate limiting (MRKT 429s on parallel).
        Only fetches listings by default; orders/feed fetched on HOT collections.
        """
        fetch_start = time.monotonic()
        # Map display name to API name (for numeric-ID collections)
        api_name = self.get_api_collection_name(collection)

        try:
            # Listings are always needed (cheapest call)
            raw_listings_resp = await self._mrkt.get_listings(
                collection_names=[api_name], count=30, ordering="Price", low_to_high=True
            )
            raw_gifts = raw_listings_resp.get("gifts", []) if raw_listings_resp else []

            if not raw_gifts:
                return None

            # Orders and feed only if we have listings (saves 2 API calls on empty collections)
            raw_orders_resp = await self._mrkt.get_orders(collection_names=[api_name], count=10)
            raw_orders = raw_orders_resp.get("orders", []) if raw_orders_resp else []

            try:
                raw_feed_resp = await self._mrkt.get_feed(
                    count=20, filters={"collectionNames": [api_name]}
                )
                raw_feed = raw_feed_resp.get("items", []) if raw_feed_resp else []
            except Exception:
                raw_feed = []

            listings = normalize_listings(raw_gifts, Market.MRKT)
            orders = normalize_orders(raw_orders)
            sales = normalize_sales(raw_feed, Market.MRKT)

            fetch_time = datetime.now(timezone.utc)
            data_age = time.monotonic() - fetch_start

            snapshot = build_snapshot(
                collection=collection,
                market=Market.MRKT,
                listings=listings,
                sales=sales,
                orders=orders,
                fetch_time=fetch_time,
            )
            snapshot.data_age_seconds = data_age

            # Cache
            self._mrkt_snapshots[collection] = snapshot
            return snapshot

        except Exception:
            logger.exception("MRKT fetch error for %s", collection)
            return None

    async def fetch_getgems_snapshot(
        self, collection: str, market: Market = Market.GETGEMS
    ) -> MarketSnapshot | None:
        """Fetch Getgems data for one collection via REST API."""
        try:
            listings_raw = await self._gg.get_gift_listings(
                count=20, sort_asc=True, collection_name=collection
            )

            if not listings_raw:
                return None

            now = datetime.now(timezone.utc)
            listings: list[Listing] = []
            for item in listings_raw:
                price = item.get("price_nanoton", 0)
                if price <= 0:
                    continue
                listings.append(
                    Listing(
                        gift_id=item.get("nft_address", ""),
                        collection=item.get("collection", collection),
                        model="",
                        symbol="",
                        backdrop="",
                        price=price,
                        seller_id="",
                        listed_at=None,
                        market=Market.GETGEMS,
                    )
                )

            floor = listings[0].price if listings else 0

            return MarketSnapshot(
                collection=collection,
                market=Market.GETGEMS,
                timestamp=now,
                listings=listings,
                recent_sales=[],
                buy_orders=[],
                floor_price=floor,
                data_age_seconds=0.0,
            )
        except Exception:
            logger.exception("Getgems fetch error for %s", collection)
            return None

    async def refresh_getgems_floors(self, collections: list[str]) -> None:
        """Bulk refresh Getgems floor prices (rate-limited)."""
        now = time.monotonic()
        if now - self._last_gg_fetch < self._gg_fetch_interval:
            return

        try:
            floors = await self._gg.get_floor_prices_bulk(collections)
            self._gg_floors = floors
            self._last_gg_fetch = now
            logger.info("Getgems floors refreshed: %d collections", len(floors))
        except Exception:
            logger.exception("Getgems bulk floor fetch failed")

    def get_cached_gg_floor(self, collection: str) -> int:
        # Exact match first
        val = self._gg_floors.get(collection, 0)
        if val > 0:
            return val

        # Getgems uses plural names (e.g. "Chill Flames" vs MRKT "Chill Flame")
        # Try common plural forms
        for suffix in ("s", "es"):
            val = self._gg_floors.get(collection + suffix, 0)
            if val > 0:
                return val

        # Try removing trailing 's' (reverse lookup)
        if collection.endswith("s"):
            val = self._gg_floors.get(collection[:-1], 0)
            if val > 0:
                return val

        # Fuzzy: case-insensitive substring match
        coll_lower = collection.lower()
        for name, price in self._gg_floors.items():
            if coll_lower in name.lower() or name.lower() in coll_lower:
                return price

        return 0

    def get_all_gg_floors(self) -> dict[str, int]:
        """Return all cached Getgems floor prices {name: nanoton}."""
        return dict(self._gg_floors)

    def get_cached_mrkt_snapshot(self, collection: str) -> MarketSnapshot | None:
        return self._mrkt_snapshots.get(collection)

    def get_all_mrkt_floors(self) -> dict[str, int]:
        """Return all cached MRKT floor prices {name: nanoton}.

        Prefers bulk floors (from /gifts/collections) over per-collection snapshots.
        """
        floors = dict(self._mrkt_bulk_floors)
        # Overlay per-collection snapshot floors (more recent for hot collections)
        for name, snap in self._mrkt_snapshots.items():
            if snap.floor_price and snap.floor_price > 0:
                floors[name] = snap.floor_price
        return floors

    async def refresh_mrkt_floors_bulk(self) -> dict[str, int]:
        """Fetch all MRKT floors in 1 request via /gifts/collections.

        Returns {name: floor_nanoton}. Updates internal cache.
        """
        now = time.monotonic()
        if now - self._last_mrkt_bulk_fetch < self._mrkt_bulk_fetch_interval:
            return dict(self._mrkt_bulk_floors)

        try:
            collections = await self._mrkt.get_collections()
            floors: dict[str, int] = {}
            for c in collections:
                name = c.get("name", "")
                title = c.get("title", name)
                floor = c.get("floorPriceNanoTons", 0)
                if not floor or floor <= 0:
                    continue
                is_numeric_id = bool(name) and name[0].isdigit()
                if is_numeric_id:
                    # Old/rare edition (numeric slug like "5839094187366024301").
                    # Only store by numeric name — never by title. This avoids
                    # overwriting the standard edition's floor with the rare
                    # edition's much higher price (e.g. Khabib's Papakha 197 TON
                    # rare vs 17.79 TON standard).
                    floors[name] = floor
                else:
                    # Standard edition — store by both title and normalized title
                    norm_title = _normalize_name(title) if title else ""
                    if title:
                        floors[title] = floor
                    if norm_title and norm_title != title:
                        floors[norm_title] = floor
                    if name and name != title:
                        floors[name] = floor
            self._mrkt_bulk_floors = floors
            self._last_mrkt_bulk_fetch = now
            logger.info("MRKT bulk floors refreshed: %d collections", len(floors))
            return floors
        except Exception:
            logger.exception("MRKT bulk floor fetch failed")
            return dict(self._mrkt_bulk_floors)

    async def refresh_mrkt_top_orders(self) -> dict[str, dict[str, Any]]:
        """Fetch top buy orders for ALL collections in 1 request.

        Returns {collection_name: order_dict}.
        """
        now = time.monotonic()
        if now - self._last_top_orders_fetch < self._top_orders_fetch_interval:
            return dict(self._mrkt_top_orders)

        try:
            orders = await self._mrkt.get_all_collection_top_orders()
            result: dict[str, dict[str, Any]] = {}
            for o in orders:
                coll_name = o.get("collectionName", "")
                # Use collectionTitle as display name (matches our collection keys)
                coll_title = o.get("collectionTitle", coll_name)
                if coll_title:
                    result[coll_title] = o
                if coll_name and coll_name != coll_title:
                    result[coll_name] = o
            self._mrkt_top_orders = result
            self._last_top_orders_fetch = now
            logger.info(
                "MRKT top orders refreshed: %d collections", len(orders)
            )
            return result
        except Exception:
            logger.exception("MRKT top orders fetch failed")
            return dict(self._mrkt_top_orders)

    def get_cached_top_order(self, collection: str) -> int:
        """Get the best (highest) buy order price for a collection. Returns nanoTON."""
        order = self._mrkt_top_orders.get(collection)
        if order:
            return order.get("priceMaxNanoTONs", 0) or 0
        return 0

    async def bulk_scan_opportunities(
        self,
    ) -> list[str]:
        """Fast bulk scan: compare floors across all markets + top orders.

        Uses bulk API calls for ALL collections.
        Returns list of collection names that have promising spreads
        (worthy of a detailed per-collection scan).
        """
        # Refresh all bulk data
        mrkt_floors = await self.refresh_mrkt_floors_bulk()
        await self.refresh_mrkt_top_orders()

        promising: set[str] = set()

        for coll_name, mrkt_floor in mrkt_floors.items():
            if mrkt_floor <= 0:
                continue

            # Check cross-market spread: MRKT floor vs Getgems floor
            gg_floor = self.get_cached_gg_floor(coll_name)
            if gg_floor > 0:
                spread_pct = (gg_floor - mrkt_floor) / mrkt_floor * 100
                if spread_pct > 5.0:
                    promising.add(coll_name)
                    continue

            # Check Portal spread: MRKT floor vs Portal floor
            portal_floor = self.get_cached_portal_floor(coll_name)
            if portal_floor > 0:
                portal_spread = (portal_floor - mrkt_floor) / mrkt_floor * 100
                if portal_spread > 3.0:
                    promising.add(coll_name)
                    continue

            # Check order arb: MRKT floor vs top buy order
            top_order_price = self.get_cached_top_order(coll_name)
            if top_order_price > 0:
                order_spread = (top_order_price - mrkt_floor) / mrkt_floor * 100
                if order_spread > 3.0:
                    promising.add(coll_name)

        # Also check Portal floors vs other markets (Portal as buy source)
        for coll_name, portal_floor in self._portal_floors.items():
            if portal_floor <= 0 or coll_name in promising:
                continue
            mrkt_floor = mrkt_floors.get(coll_name, 0)
            gg_floor = self.get_cached_gg_floor(coll_name)
            # Portal cheaper than MRKT?
            if mrkt_floor > 0:
                spread = (mrkt_floor - portal_floor) / portal_floor * 100
                if spread > 3.0:
                    promising.add(coll_name)
                    continue
            # Portal cheaper than Getgems?
            if gg_floor > 0:
                spread = (gg_floor - portal_floor) / portal_floor * 100
                if spread > 3.0:
                    promising.add(coll_name)

        result = list(promising)
        logger.info(
            "Bulk scan: %d MRKT, %d GG, %d Portal floors, %d promising",
            len(mrkt_floors),
            len(self._gg_floors),
            len(self._portal_floors),
            len(result),
        )
        return result

    # ── Fragment ──────────────────────────────────────────────────────

    async def refresh_fragment_floors(self, collections: list[str]) -> None:
        """Bulk refresh Fragment floor prices (rate-limited)."""
        now = time.monotonic()
        if now - self._last_fragment_fetch < self._fragment_fetch_interval:
            return

        if not self._fragment or not self._fragment.ready:
            return

        try:
            floors = await self._fragment.get_floor_prices_bulk(collections)
            self._fragment_floors = floors
            self._last_fragment_fetch = now
            logger.info("Fragment floors refreshed: %d collections", len(floors))
        except Exception:
            logger.exception("Fragment bulk floor fetch failed")

    def get_cached_fragment_floor(self, collection: str) -> int:
        """Get cached Fragment floor price for a collection."""
        if not self._fragment:
            return 0
        val = self._fragment_floors.get(collection, 0)
        if val > 0:
            return val
        # Try the fragment client's own cache (with fuzzy matching)
        return self._fragment.get_cached_floor(collection)

    def get_all_fragment_floors(self) -> dict[str, int]:
        """Return all cached Fragment floor prices {name: nanoton}."""
        return dict(self._fragment_floors)

    # ── Portal ────────────────────────────────────────────────────────

    async def refresh_portal_floors(self) -> dict[str, int]:
        """Bulk refresh Portal floor prices (1 request for all collections)."""
        now = time.monotonic()
        if now - self._last_portal_fetch < self._portal_fetch_interval:
            return dict(self._portal_floors)

        if not self._portal:
            return {}

        try:
            floors = await self._portal.get_collection_floors_bulk()
            self._portal_floors = floors
            self._last_portal_fetch = now
            logger.info("Portal floors refreshed: %d collections", len(floors))
            return floors
        except Exception:
            logger.exception("Portal bulk floor fetch failed")
            return dict(self._portal_floors)

    def get_cached_portal_floor(self, collection: str) -> int:
        """Get cached Portal floor price for a collection."""
        if not self._portal:
            return 0
        val = self._portal_floors.get(collection, 0)
        if val > 0:
            return val
        # Fuzzy match: try normalized name
        norm = _normalize_name(collection)
        for k, v in self._portal_floors.items():
            if _normalize_name(k) == norm:
                return v
        return 0

    def get_all_portal_floors(self) -> dict[str, int]:
        """Return all cached Portal floor prices {name: nanoton}."""
        return dict(self._portal_floors)

    async def fetch_mrkt_floor(self, collection: str) -> int:
        """Fetch MRKT floor price for a single collection. Returns nanoton or 0."""
        try:
            listings = await self._mrkt.get_listings(
                collection_names=[collection],
                count=1,
                ordering="Price",
                low_to_high=True,
            )
            gifts = listings.get("gifts", [])
            if gifts:
                return gifts[0].get("salePrice", 0) or 0
        except Exception:
            pass
        return 0

    def get_api_collection_name(self, display_name: str) -> str:
        """Map display name → MRKT API name (for numeric-ID collections)."""
        return self._title_to_api_name.get(display_name, display_name)

    async def load_collections(self) -> list[str]:
        """Load all available collection names from MRKT.

        Uses title for numeric-ID collections (e.g. Durov's Boots).
        Stores name↔title mapping for API calls.
        """
        try:
            collections = await self._mrkt.get_collections()
            names: list[str] = []
            for c in collections:
                name = c.get("name", "")
                title = c.get("title", name)
                if not name:
                    continue
                # Build bidirectional mapping
                if name != title and title:
                    self._title_to_api_name[title] = name
                    self._api_name_to_title[name] = title
                # Use title for display-friendly key (matches our internal naming)
                if name.isdigit() and title:
                    names.append(title)
                else:
                    names.append(name)
            logger.info("Loaded %d collections from MRKT", len(names))
            return names
        except Exception:
            logger.exception("Failed to load collections")
            return []

    def find_cross_market_opportunities(
        self,
        mrkt_snapshot: MarketSnapshot,
        gg_floor: int,
        shadow_mode: bool = True,
        fragment_floor: int = 0,
        portal_floor: int = 0,
    ) -> list[Opportunity]:
        """Find arbitrage: buy on MRKT, sell on best-priced market.

        Sell targets: Getgems (0% fee), Portal (0% fee + 0.25 TON withdrawal).
        Fragment excluded as sell target (14-day NFT export hold).
        """
        if not mrkt_snapshot.listings:
            return []

        # Pick best sell target: highest net revenue
        sell_candidates: list[tuple[int, Market, str]] = []
        if gg_floor > 0:
            sell_candidates.append((gg_floor, Market.GETGEMS, "getgems"))
        if portal_floor > 0:
            sell_candidates.append((portal_floor, Market.PORTAL, "portal"))

        if not sell_candidates:
            return []

        # Sort by floor descending — best sell price first
        sell_candidates.sort(key=lambda x: x[0], reverse=True)
        best_floor, sell_market, sell_market_name = sell_candidates[0]

        now = datetime.now(timezone.utc)
        opportunities: list[Opportunity] = []

        # Target sell price: slightly below best floor to sell fast
        target_sell = int(best_floor * 0.98)  # 2% below floor

        # Only consider the cheapest listing (best ROI) to avoid duplicate deals
        listing = mrkt_snapshot.listings[0]
        pnl = calculate_pnl(
            buy_price=listing.price,
            expected_sell_price=target_sell,
            expected_hold_hours=24.0,
            needs_transfer=True,
            sell_market=sell_market_name,
        )

        from bot.interface.telegram_bot import get_runtime

        if pnl.roi_pct < get_runtime("cross_roi_pct"):
            return []

        deadline = DEADLINE_COLD_SEC
        if len(mrkt_snapshot.recent_sales) >= 5:
            deadline = DEADLINE_HOT_SEC
        elif len(mrkt_snapshot.recent_sales) >= 2:
            deadline = DEADLINE_WARM_SEC

        opportunities.append(
            Opportunity(
                gift_id=listing.gift_id,
                collection=listing.collection,
                model=listing.model,
                symbol=listing.symbol,
                backdrop=listing.backdrop,
                listing_price=listing.price,
                strategy=Strategy.CROSS_MARKET,
                buy_market=Market.MRKT,
                sell_market=sell_market,
                target_sell_price=target_sell,
                order_id=None,
                pnl=pnl,
                score=None,
                detected_at=now,
                expires_at=now + timedelta(seconds=deadline),
                snapshot_id=None,
            )
        )

        return opportunities

    def find_order_arb_opportunities(
        self,
        mrkt_snapshot: MarketSnapshot,
    ) -> list[Opportunity]:
        """Find arbitrage: buy listing → fill order on MRKT.

        Opportunity: order price > listing price + fees.
        """
        if not mrkt_snapshot.listings or not mrkt_snapshot.buy_orders:
            return []

        now = datetime.now(timezone.utc)
        opportunities: list[Opportunity] = []

        for order in mrkt_snapshot.buy_orders:
            remaining = order.quantity_total - order.quantity_filled
            if remaining <= 0:
                continue

            # Find the cheapest listing that matches this order
            for listing in mrkt_snapshot.listings:
                if order.model and listing.model != order.model:
                    continue
                if order.symbol and listing.symbol != order.symbol:
                    continue
                if order.backdrop and listing.backdrop != order.backdrop:
                    continue
                if listing.price > order.price_max:
                    continue

                pnl = calculate_pnl(
                    buy_price=listing.price,
                    expected_sell_price=order.price_max,
                    expected_hold_hours=0.1,
                    needs_transfer=False,
                )

                from bot.interface.telegram_bot import get_runtime

                if pnl.roi_pct < get_runtime("order_roi_pct"):
                    break  # listings sorted by price — if cheapest fails, rest will too

                opportunities.append(
                    Opportunity(
                        gift_id=listing.gift_id,
                        collection=listing.collection,
                        model=listing.model,
                        symbol=listing.symbol,
                        backdrop=listing.backdrop,
                        listing_price=listing.price,
                        strategy=Strategy.MRKT_ORDER_ARB,
                        buy_market=Market.MRKT,
                        sell_market=Market.MRKT,
                        target_sell_price=order.price_max,
                        order_id=order.order_id,
                        pnl=pnl,
                        score=None,
                        detected_at=now,
                        expires_at=now + timedelta(seconds=DEADLINE_HOT_SEC),
                        snapshot_id=None,
                    )
                )
                break  # one listing per order

        return opportunities

    def find_deep_discount_opportunities(
        self,
        mrkt_snapshot: MarketSnapshot,
    ) -> list[Opportunity]:
        """Find deep discounts: listing far below fair value."""
        if not mrkt_snapshot.listings:
            return []

        pricing = self._pricing.price(mrkt_snapshot)
        if pricing.confidence < 0.3:
            return []  # not enough data to determine fair value

        now = datetime.now(timezone.utc)
        opportunities: list[Opportunity] = []
        sell_hours = self._liquidation.expected_sell_hours(pricing.fair_value, mrkt_snapshot)

        # Only consider the cheapest listing to avoid duplicate deals
        listing = mrkt_snapshot.listings[0]
        discount = (pricing.fair_value - listing.price) / pricing.fair_value
        if discount < 0.15:
            return []

        target_sell = int(pricing.fair_value * 0.95)

        pnl = calculate_pnl(
            buy_price=listing.price,
            expected_sell_price=target_sell,
            expected_hold_hours=sell_hours,
            needs_transfer=False,
        )

        from bot.interface.telegram_bot import get_runtime

        if pnl.roi_pct < get_runtime("deep_roi_pct"):
            return []

        opportunities.append(
            Opportunity(
                gift_id=listing.gift_id,
                collection=listing.collection,
                model=listing.model,
                symbol=listing.symbol,
                backdrop=listing.backdrop,
                listing_price=listing.price,
                strategy=Strategy.DEEP_DISCOUNT,
                buy_market=Market.MRKT,
                sell_market=Market.MRKT,
                target_sell_price=target_sell,
                order_id=None,
                pnl=pnl,
                score=None,
                detected_at=now,
                expires_at=now + timedelta(seconds=DEADLINE_WARM_SEC),
                snapshot_id=None,
            )
        )

        return opportunities
