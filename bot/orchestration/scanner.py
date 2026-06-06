"""Adaptive scanner with hot/cold tiers and shadow mode.

Tier assignment:
  HOT  (2-5s)   — collections with recent opportunities, high velocity
  WARM (10-30s)  — moderate activity
  COLD (60-120s) — low activity, rarely profitable

Priority queue: higher score = scanned sooner.
Score = recent_opportunity_count x liquidity_score x profit_potential

Shadow mode: finds opportunities, simulates execution, tracks
hypothetical PnL without spending real money.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from bot.models.types import Market, MarketSnapshot, ScanTier

if TYPE_CHECKING:
    from bot.data.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

# Tier intervals (seconds)
TIER_INTERVALS: dict[ScanTier, tuple[float, float]] = {
    ScanTier.HOT: (2.0, 5.0),
    ScanTier.WARM: (10.0, 30.0),
    ScanTier.COLD: (60.0, 120.0),
}

TIER_RECLASSIFY_INTERVAL = 300  # 5 minutes


class CollectionPriority:
    """Tracks per-collection scanning priority."""

    def __init__(self, collection: str) -> None:
        self.collection = collection
        self.tier = ScanTier.COLD
        self.last_scanned_at: float = 0.0
        self.next_scan_at: float = 0.0
        self.opportunity_count_1h: int = 0
        self.last_opportunity_at: float = 0.0
        self.liquidity_score: float = 0.0
        self.scan_count: int = 0
        self.error_count: int = 0

    @property
    def priority_score(self) -> float:
        """Higher = should be scanned sooner."""
        recency_bonus = 0.0
        if self.last_opportunity_at > 0:
            age = time.monotonic() - self.last_opportunity_at
            recency_bonus = max(10.0 - age / 60, 0)  # bonus for last 10 min

        return self.opportunity_count_1h * 5 + self.liquidity_score * 3 + recency_bonus

    @property
    def interval(self) -> float:
        lo, hi = TIER_INTERVALS[self.tier]
        # More active → faster within tier
        if self.priority_score > 5:
            return lo
        return hi


# Type for the fetch callback
FetchCallback = Callable[
    [str, Market],
    Coroutine[Any, Any, MarketSnapshot | None],
]

# Type for the opportunity callback
OpportunityCallback = Callable[
    [MarketSnapshot, MarketSnapshot | None],
    Coroutine[Any, Any, list[dict[str, Any]]],
]


class AdaptiveScanner:
    # Max time for a single collection scan (API timeout protection)
    SCAN_TIMEOUT_SEC = 45.0

    def __init__(
        self,
        collections: list[str],
        fetch_mrkt: FetchCallback,
        fetch_getgems: FetchCallback,
        on_opportunities: OpportunityCallback,
        shadow_mode: bool = True,
        runtime_getter: Callable[[str], float] | None = None,
        mds: MarketDataService | None = None,
    ) -> None:
        self._priorities: dict[str, CollectionPriority] = {}
        now = time.monotonic()
        for i, c in enumerate(collections):
            p = CollectionPriority(c)
            # Stagger initial scans by 1s each — all collections scanned within ~2 min
            p.next_scan_at = now + i * 1.0
            self._priorities[c] = p
        self._fetch_mrkt = fetch_mrkt
        self._fetch_getgems = fetch_getgems
        self._on_opportunities = on_opportunities
        self._shadow_mode = shadow_mode
        self._running = False
        self._paused = False
        self._last_reclassify = 0.0
        self._runtime_getter = runtime_getter

        # Snapshot cache for reconciliation
        self._prev_snapshots: dict[str, MarketSnapshot] = {}

        # Stats
        self._cycles = 0
        self._opportunities_found = 0
        self._error_count = 0

        # Activity log (last 50 events for UI)
        self._activity_log: deque[dict[str, Any]] = deque(maxlen=50)
        self._last_scan_collection: str = ""
        self._last_scan_time: float = 0.0

        # Market buy toggles (scanner only cares about buying)
        self._markets_buy_enabled: dict[str, bool] = {
            "mrkt": True,
            "getgems": True,
            "fragment": True,
            "portal": True,
        }

        # Bulk scan support
        self._mds = mds
        self._last_bulk_scan: float = 0.0
        self._bulk_scan_interval = 30.0  # seconds between bulk scans
        self._promising_collections: set[str] = set()
        self._bulk_scans_done: int = 0

    def set_market_enabled(self, market: str, enabled: bool) -> None:
        self._markets_buy_enabled[market] = enabled
        logger.info("Scanner market %s buy=%s", market, enabled)

    def set_market_buy_enabled(self, market: str, enabled: bool) -> None:
        self._markets_buy_enabled[market] = enabled
        logger.info("Scanner market %s buy=%s", market, enabled)

    def is_market_enabled(self, market: str) -> bool:
        return self._markets_buy_enabled.get(market, True)

    def is_market_buy_enabled(self, market: str) -> bool:
        return self._markets_buy_enabled.get(market, True)

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        self._shadow_mode = value
        logger.info("Shadow mode: %s", "ON" if value else "OFF")

    async def start(self) -> None:
        """Main scan loop with bulk + targeted scanning.

        Phase 1 (bulk): Every 30s, use bulk APIs to refresh ALL floors
        and top orders (2-3 API calls total). Identifies promising collections.

        Phase 2 (targeted): Only scan collections with promising spreads
        using per-collection API calls (listings + orders + feed).
        """
        self._running = True
        logger.info(
            "Scanner started: %d collections, shadow=%s",
            len(self._priorities),
            self._shadow_mode,
        )

        while self._running:
            try:
                if self._paused:
                    await asyncio.sleep(2.0)
                    continue

                now = time.monotonic()

                # ── Phase 1: Bulk scan (every 30s) ──────────────────
                if self._mds and now - self._last_bulk_scan >= self._bulk_scan_interval:
                    try:
                        promising = await asyncio.wait_for(
                            self._mds.bulk_scan_opportunities(),
                            timeout=30.0,
                        )
                        self._promising_collections = set(promising)
                        self._last_bulk_scan = time.monotonic()
                        self._bulk_scans_done += 1

                        # Promote promising collections — but only if not
                        # scanned recently (avoids re-scanning the same 14
                        # while 111 others never get a turn).
                        promote_now = time.monotonic()
                        for coll in promising:
                            prio = self._priorities.get(coll)
                            if prio:
                                staleness = promote_now - prio.last_scanned_at
                                if staleness > prio.interval or prio.scan_count == 0:
                                    prio.next_scan_at = 0.0
                                if prio.tier == ScanTier.COLD:
                                    prio.tier = ScanTier.WARM
                    except asyncio.TimeoutError:
                        logger.warning("Bulk scan timeout (>30s)")
                    except Exception:
                        logger.exception("Bulk scan error")

                # ── Phase 2: Targeted per-collection scan ────────────
                collection = self._next_collection()
                if collection is None:
                    await asyncio.sleep(1.0)
                    continue

                try:
                    await asyncio.wait_for(
                        self._scan_one(collection),
                        timeout=self.SCAN_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Scan timeout for %s (>%.0fs)", collection, self.SCAN_TIMEOUT_SEC
                    )
                    prio = self._priorities.get(collection)
                    if prio:
                        prio.error_count += 1
                        prio.next_scan_at = time.monotonic() + 60.0
                self._cycles += 1

                # Delay between per-collection scans (shorter for promising)
                is_promising = collection in self._promising_collections
                if is_promising:
                    await asyncio.sleep(0.5)  # fast scan for promising
                else:
                    scan_interval = 30.0
                    if self._runtime_getter:
                        scan_interval = self._runtime_getter("scan_interval") or 30.0
                    n_colls = max(len(self._priorities), 1)
                    delay = max(scan_interval / n_colls, 0.5)
                    await asyncio.sleep(delay)

                # Reclassify tiers periodically
                now_t = time.monotonic()
                if now_t - self._last_reclassify > TIER_RECLASSIFY_INTERVAL:
                    self._reclassify_tiers()
                    self._last_reclassify = now_t

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scanner cycle error")
                await asyncio.sleep(5.0)

        logger.info("Scanner stopped after %d cycles", self._cycles)

    def stop(self) -> None:
        self._running = False

    def _log_activity(self, event: str, collection: str = "", **extra: Any) -> None:
        self._activity_log.append(
            {
                "time": time.time(),
                "event": event,
                "collection": collection,
                **extra,
            }
        )

    async def _scan_one(self, collection: str) -> None:
        """Scan one collection — MRKT + Getgems for cross-market arbitrage."""
        prio = self._priorities[collection]
        now = time.monotonic()
        prio.last_scanned_at = now
        prio.scan_count += 1
        self._last_scan_collection = collection
        self._last_scan_time = time.time()

        try:
            mrkt_snap: MarketSnapshot | None = None
            if self.is_market_enabled("mrkt"):
                try:
                    mrkt_snap = await self._fetch_mrkt(collection, Market.MRKT)
                except Exception as e:
                    logger.warning("MRKT fetch failed for %s: %s", collection, e)
                    prio.error_count += 1
                    self._log_activity("error", collection, msg=f"MRKT: {e}")

            if mrkt_snap is None:
                prio.next_scan_at = now + prio.interval
                return

            # Fetch Getgems data (non-blocking — don't fail the scan if Getgems is down)
            gg_snap: MarketSnapshot | None = None
            if self.is_market_enabled("getgems"):
                try:
                    gg_snap = await self._fetch_getgems(collection, Market.GETGEMS)
                except Exception as e:
                    logger.debug("Getgems fetch failed for %s: %s", collection, e)

            # Attach Getgems floor to MRKT snapshot for cross-market comparison
            if gg_snap and gg_snap.floor_price > 0:
                mrkt_snap.getgems_floor = gg_snap.floor_price

            # Log scan result
            self._log_activity(
                "scan",
                collection,
                mrkt_floor=mrkt_snap.floor_price,
                gg_floor=mrkt_snap.getgems_floor,
                listings=len(mrkt_snap.listings),
                tier=prio.tier.value,
            )

            # Find opportunities
            prev = self._prev_snapshots.get(collection)
            opportunities = await self._on_opportunities(mrkt_snap, prev)

            if opportunities:
                self._opportunities_found += len(opportunities)
                prio.opportunity_count_1h += len(opportunities)
                prio.last_opportunity_at = now
                for opp in opportunities:
                    self._log_activity(
                        "opportunity",
                        collection,
                        type=opp.get("type", "?"),
                        deal_id=opp.get("deal_id"),
                    )

            # Cache for next cycle
            self._prev_snapshots[collection] = mrkt_snap

            # Update liquidity score
            prio.liquidity_score = min(len(mrkt_snap.listings) / 10, 1.0)

            # Schedule next scan
            prio.next_scan_at = now + prio.interval

        except Exception:
            logger.exception("Scan error for %s", collection)
            prio.error_count += 1
            prio.next_scan_at = now + 30.0  # back off on error

    def _next_collection(self) -> str | None:
        """Pick the collection most overdue for scanning.

        Never-scanned collections are always preferred over already-scanned
        ones to ensure a full initial sweep before re-scanning.
        """
        now = time.monotonic()
        candidates = [p for p in self._priorities.values() if p.next_scan_at <= now]

        if not candidates:
            return None

        # Prefer never-scanned, then sort by priority score
        candidates.sort(
            key=lambda p: (p.scan_count > 0, -p.priority_score),
        )
        return candidates[0].collection

    def _reclassify_tiers(self) -> None:
        """Move collections between tiers based on recent activity."""
        for prio in self._priorities.values():
            # Decay opportunity count
            prio.opportunity_count_1h = max(prio.opportunity_count_1h - 1, 0)

            # Assign tier
            if prio.opportunity_count_1h >= 3 or prio.priority_score > 8:
                new_tier = ScanTier.HOT
            elif prio.opportunity_count_1h >= 1 or prio.priority_score > 3:
                new_tier = ScanTier.WARM
            else:
                new_tier = ScanTier.COLD

            if new_tier != prio.tier:
                logger.info(
                    "Tier change: %s %s → %s",
                    prio.collection,
                    prio.tier.value,
                    new_tier.value,
                )
                prio.tier = new_tier

    def update_collections(self, collections: list[str]) -> None:
        """Add new collections or remove stale ones."""
        current = set(self._priorities.keys())
        new = set(collections)

        for c in new - current:
            self._priorities[c] = CollectionPriority(c)

        for c in current - new:
            del self._priorities[c]

    def get_status(self) -> dict[str, Any]:
        tiers: dict[str, int] = defaultdict(int)
        scanned_count = 0
        total_errors = 0
        for p in self._priorities.values():
            tiers[p.tier.value] += 1
            if p.scan_count > 0:
                scanned_count += 1
            total_errors += p.error_count

        return {
            "running": self._running,
            "paused": self._paused,
            "shadow_mode": self._shadow_mode,
            "markets_enabled": {
                m: {"buy": self._markets_buy_enabled.get(m, True)}
                for m in ("mrkt", "getgems", "fragment", "portal")
            },
            "total_collections": len(self._priorities),
            "scanned_collections": scanned_count,
            "cycles": self._cycles,
            "opportunities_found": self._opportunities_found,
            "tiers": dict(tiers),
            "errors": total_errors,
            "last_scan": self._last_scan_collection,
            "last_scan_time": self._last_scan_time,
            "bulk_scans": self._bulk_scans_done,
            "promising_collections": len(self._promising_collections),
            "activity": list(self._activity_log)[-10:],  # last 10 events
        }
