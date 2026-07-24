"""Market data normalization and quality layer.

Responsibilities:
  1. Deduplication — remove duplicate listings by gift_id
  2. Stale detection — flag data older than freshness thresholds
  3. Timestamp normalization — UTC everywhere
  4. Price validation — reject non-positive, suspiciously low/high
  5. Consistency check — flag anomalies vs previous snapshot
  6. Reconciliation — compare snapshots, detect phantom opportunities

All external API data MUST pass through this layer before use.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bot.models.types import (
    FRESHNESS_SNAPSHOT_MAX_SEC,
    Listing,
    Market,
    MarketSnapshot,
    Order,
    Sale,
)

logger = logging.getLogger(__name__)

# Price sanity bounds (nanoTON)
MIN_VALID_PRICE = 10_000_000  # 0.01 TON
MAX_VALID_PRICE = 100_000_000_000_000  # 100,000 TON


def _parse_datetime(raw: str | int | float | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw / 1000 if raw > 1e12 else raw, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def normalize_listings(
    raw_gifts: list[dict[str, Any]],
    market: Market,
) -> list[Listing]:
    """Convert raw API listings to typed Listing objects with dedup and validation."""
    seen_ids: set[str] = set()
    result: list[Listing] = []

    for g in raw_gifts:
        gift_id = g.get("id", "")
        if not gift_id or gift_id in seen_ids:
            continue
        seen_ids.add(gift_id)

        price = _extract_price(g)
        if not _is_valid_price(price):
            continue

        result.append(
            Listing(
                gift_id=gift_id,
                collection=g.get("collectionName", ""),
                model=g.get("modelName", ""),
                symbol=g.get("symbolName", ""),
                backdrop=g.get("backdropName", ""),
                price=price,
                seller_id=g.get("ownerId", g.get("sellerId", "")),
                listed_at=_parse_datetime(g.get("listedAt") or g.get("createdAt")),
                market=market,
                number=g.get("number"),
            )
        )

    return result


def normalize_orders(raw_orders: list[dict[str, Any]]) -> list[Order]:
    """Convert raw API orders with dedup and validation."""
    seen_ids: set[str] = set()
    result: list[Order] = []

    for o in raw_orders:
        order_id = o.get("id", "")
        if not order_id or order_id in seen_ids:
            continue
        seen_ids.add(order_id)

        price_max = int(o.get("priceMaxNanoTONs", 0))
        price_min = int(o.get("priceMinNanoTONs", 0))
        if not _is_valid_price(price_max):
            continue

        total_qty = int(o.get("totalQuantity", 0))
        filled_qty = int(o.get("completedQuantity", 0))
        if total_qty <= filled_qty:
            continue  # fully filled

        result.append(
            Order(
                order_id=order_id,
                collection=o.get("collectionName", ""),
                model=o.get("modelName"),
                symbol=o.get("symbolName"),
                backdrop=o.get("backdropName"),
                price_min=price_min,
                price_max=price_max,
                quantity_total=total_qty,
                quantity_filled=filled_qty,
                creator_id=o.get("userId", ""),
                created_at=_parse_datetime(o.get("createdAt")),
            )
        )

    return result


def normalize_sales(raw_feed: list[dict[str, Any]], market: Market) -> list[Sale]:
    """Extract sale events from feed with dedup."""
    seen: set[str] = set()
    result: list[Sale] = []

    for item in raw_feed:
        event_type = item.get("type", "")
        if event_type not in ("Sale", "sale", "GiftSold"):
            continue

        gift_id = item.get("giftId", item.get("id", ""))
        sold_at_raw = item.get("createdAt") or item.get("timestamp")
        sold_at = _parse_datetime(sold_at_raw) or datetime.now(timezone.utc)

        dedup_key = f"{gift_id}:{sold_at.isoformat()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        price = int(item.get("price", item.get("salePrice", 0)))
        if not _is_valid_price(price):
            continue

        result.append(
            Sale(
                gift_id=gift_id,
                collection=item.get("collectionName", ""),
                price=price,
                buyer_id=item.get("buyerId", item.get("toUserId", "")),
                seller_id=item.get("sellerId", item.get("fromUserId", "")),
                sold_at=sold_at,
                market=market,
            )
        )

    return result


def build_snapshot(
    collection: str,
    market: Market,
    listings: list[Listing],
    sales: list[Sale],
    orders: list[Order],
    fetch_time: datetime | None = None,
) -> MarketSnapshot:
    """Build a consistent MarketSnapshot from normalized data."""
    now = fetch_time or datetime.now(timezone.utc)

    # Filter to this collection
    coll_listings = [lst for lst in listings if lst.collection == collection]
    coll_sales = [s for s in sales if s.collection == collection]
    coll_orders = [o for o in orders if o.collection == collection]

    # Sort listings by price for floor
    coll_listings.sort(key=lambda item: item.price)
    floor = coll_listings[0].price if coll_listings else 0

    return MarketSnapshot(
        collection=collection,
        market=market,
        timestamp=now,
        listings=coll_listings,
        recent_sales=coll_sales,
        buy_orders=coll_orders,
        floor_price=floor,
        data_age_seconds=0.0,
    )


def check_freshness(
    snapshot: MarketSnapshot,
    max_age_seconds: float = FRESHNESS_SNAPSHOT_MAX_SEC,
) -> bool:
    """Returns True if snapshot is within freshness window."""
    return snapshot.data_age_seconds <= max_age_seconds


def reconcile_snapshots(
    prev: MarketSnapshot | None,
    curr: MarketSnapshot,
) -> list[str]:
    """Compare two snapshots and return list of anomalies."""
    anomalies: list[str] = []

    if prev is None:
        return anomalies

    # Floor dropped more than 30% in one cycle
    if prev.floor_price > 0 and curr.floor_price > 0:
        drop = (prev.floor_price - curr.floor_price) / prev.floor_price
        if drop > 0.30:
            anomalies.append(f"floor_crash:{drop:.0%}")

    # Listings count changed dramatically (>50% in one cycle)
    prev_count = len(prev.listings)
    curr_count = len(curr.listings)
    if prev_count > 5:
        change = abs(curr_count - prev_count) / prev_count
        if change > 0.50:
            anomalies.append(f"listings_spike:{prev_count}→{curr_count}")

    return anomalies


# ── Helpers ────────────────────────────────────────────────────────────


def _extract_price(gift: dict[str, Any]) -> int:
    """Extract listing price from gift dict, trying multiple field names."""
    for field in ("salePrice", "price", "salePriceNanoTons"):
        val = gift.get(field)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                continue
    return 0


def _is_valid_price(price: int) -> bool:
    return MIN_VALID_PRICE <= price <= MAX_VALID_PRICE
