"""Market-oriented circuit breakers.

Trips on MARKET conditions, not just infrastructure failures:
  - Volatility spike (floor swings > threshold in short window)
  - Spread explosion (bid-ask spread too wide)
  - Sales collapse (zero sales for previously active collection)
  - Inventory aging spike (too many stuck items)
  - API failures / high latency

Two levels:
  - Global circuit: pauses ALL trading
  - Per-collection circuit: blocks specific collection

Auto-closes after cooldown period.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.models.types import CircuitState, MarketSnapshot

logger = logging.getLogger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        *,
        max_consecutive_failures: int = 3,
        max_api_latency_ms: int = 10_000,
        volatility_spike_pct: float = 0.30,
        spread_explosion_pct: float = 0.40,
        sales_collapse_hours: float = 4.0,
        min_expected_sales_per_4h: int = 2,
        max_stuck_items: int = 5,
        cooldown_seconds: int = 300,
    ) -> None:
        self._max_failures = max_consecutive_failures
        self._max_latency = max_api_latency_ms
        self._vol_threshold = volatility_spike_pct
        self._spread_threshold = spread_explosion_pct
        self._sales_collapse_hours = sales_collapse_hours
        self._min_sales_4h = min_expected_sales_per_4h
        self._max_stuck = max_stuck_items
        self._cooldown = timedelta(seconds=cooldown_seconds)

        # Global state
        self._global = CircuitState(cooldown_seconds=cooldown_seconds)

        # Per-collection state
        self._collection_states: dict[str, CircuitState] = {}

        # Tracking
        self._consecutive_failures = 0
        self._recent_latencies: list[float] = []

    @property
    def is_globally_open(self) -> bool:
        self._maybe_auto_close_global()
        return self._global.is_open

    def is_collection_blocked(self, collection: str) -> bool:
        state = self._collection_states.get(collection)
        if state is None:
            return False
        self._maybe_auto_close_collection(collection)
        return state.is_open

    def can_trade(self, collection: str | None = None) -> tuple[bool, str]:
        """Check if trading is allowed. Returns (allowed, reason)."""
        if self.is_globally_open:
            return False, f"global_circuit_open:{self._global.reason}"
        if collection and self.is_collection_blocked(collection):
            state = self._collection_states[collection]
            return False, f"collection_blocked:{state.reason}"
        return True, "ok"

    # ── Event handlers ─────────────────────────────────────────────────

    def on_trade_success(self) -> None:
        self._consecutive_failures = 0

    def on_trade_failure(self, is_hard: bool = False) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            self._open_global(f"consecutive_failures:{self._consecutive_failures}")

    def on_api_latency(self, latency_ms: float) -> None:
        self._recent_latencies.append(latency_ms)
        if len(self._recent_latencies) > 100:
            self._recent_latencies = self._recent_latencies[-50:]

        if latency_ms > self._max_latency:
            self._open_global(f"high_latency:{latency_ms:.0f}ms")

    def on_stuck_items_count(self, count: int) -> None:
        if count > self._max_stuck:
            self._open_global(f"stuck_items:{count}")

    # ── Market health checks ───────────────────────────────────────────

    def check_collection_health(
        self,
        collection: str,
        current: MarketSnapshot,
        previous: MarketSnapshot | None = None,
    ) -> bool:
        """Check if a collection is safe to trade. Returns False if blocked."""
        reasons: list[str] = []

        # Spread explosion
        if current.spread_pct > self._spread_threshold:
            reasons.append(f"spread:{current.spread_pct:.0%}")

        # Volatility spike (needs previous snapshot)
        if previous and previous.floor_price > 0 and current.floor_price > 0:
            change = abs(current.floor_price - previous.floor_price) / previous.floor_price
            if change > self._vol_threshold:
                reasons.append(f"volatility:{change:.0%}")

        # Sales collapse
        recent_sales_count = len(
            [
                s
                for s in current.recent_sales
                if (current.timestamp - s.sold_at).total_seconds()
                < self._sales_collapse_hours * 3600
            ]
        )
        if len(current.recent_sales) > 0 and recent_sales_count < self._min_sales_4h:
            # Had sales before but now collapsed
            reasons.append(f"sales_collapse:{recent_sales_count}_in_{self._sales_collapse_hours}h")

        if reasons:
            reason_str = "; ".join(reasons)
            self._open_collection(collection, reason_str)
            return False

        return True

    def check_market_health(
        self,
        snapshots: dict[str, MarketSnapshot],
        prev_snapshots: dict[str, MarketSnapshot] | None = None,
    ) -> CircuitState:
        """Evaluate overall market health across all collections."""
        blocked_count = 0
        for name, snap in snapshots.items():
            prev = prev_snapshots.get(name) if prev_snapshots else None
            if not self.check_collection_health(name, snap, prev):
                blocked_count += 1

        # If majority of collections are blocked → global circuit
        total = len(snapshots)
        if total > 0 and blocked_count / total > 0.5:
            self._open_global(f"market_wide_stress:{blocked_count}/{total}_blocked")

        return self._global

    # ── Reset ──────────────────────────────────────────────────────────

    def reset_global(self) -> None:
        self._global.is_open = False
        self._global.reason = ""
        self._global.opened_at = None
        self._consecutive_failures = 0
        logger.info("Global circuit breaker manually reset")

    def reset_collection(self, collection: str) -> None:
        if collection in self._collection_states:
            del self._collection_states[collection]

    # ── Internals ──────────────────────────────────────────────────────

    def _open_global(self, reason: str) -> None:
        if self._global.is_open:
            return  # already open
        self._global.is_open = True
        self._global.reason = reason
        self._global.opened_at = datetime.now(timezone.utc)
        logger.warning("GLOBAL CIRCUIT OPEN: %s", reason)

    def _open_collection(self, collection: str, reason: str) -> None:
        state = self._collection_states.get(collection)
        if state and state.is_open:
            return

        self._collection_states[collection] = CircuitState(
            is_open=True,
            reason=reason,
            opened_at=datetime.now(timezone.utc),
            cooldown_seconds=self._cooldown.seconds,
        )
        logger.warning("Collection circuit OPEN: %s — %s", collection, reason)

    def _maybe_auto_close_global(self) -> None:
        if not self._global.is_open or self._global.opened_at is None:
            return
        if datetime.now(timezone.utc) - self._global.opened_at > self._cooldown:
            logger.info("Global circuit auto-closing after cooldown")
            self.reset_global()

    def _maybe_auto_close_collection(self, collection: str) -> None:
        state = self._collection_states.get(collection)
        if not state or not state.is_open or state.opened_at is None:
            return
        if datetime.now(timezone.utc) - state.opened_at > self._cooldown:
            logger.info("Collection circuit auto-closing: %s", collection)
            self.reset_collection(collection)

    def get_status(self) -> dict[str, Any]:
        """Snapshot of all circuit breaker states for observability."""
        blocked_collections = {
            name: state.reason for name, state in self._collection_states.items() if state.is_open
        }
        p95 = 0.0
        if self._recent_latencies:
            sorted_lat = sorted(self._recent_latencies)
            idx = int(len(sorted_lat) * 0.95)
            p95 = sorted_lat[min(idx, len(sorted_lat) - 1)]

        return {
            "global_open": self._global.is_open,
            "global_reason": self._global.reason,
            "consecutive_failures": self._consecutive_failures,
            "api_latency_p95_ms": p95,
            "blocked_collections": blocked_collections,
            "blocked_count": len(blocked_collections),
        }
