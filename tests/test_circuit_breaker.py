"""Tests for circuit breaker — market protection system."""

from __future__ import annotations

from datetime import datetime, timezone

from bot.core.circuit_breaker import CircuitBreaker
from bot.models.types import Market, MarketSnapshot


class TestCircuitBreaker:
    def setup_method(self) -> None:
        self.cb = CircuitBreaker(
            max_consecutive_failures=3,
            cooldown_seconds=1,
        )

    def test_initially_closed(self) -> None:
        assert not self.cb.is_globally_open
        allowed, reason = self.cb.can_trade()
        assert allowed
        assert reason == "ok"

    def test_opens_after_consecutive_failures(self) -> None:
        for _ in range(3):
            self.cb.on_trade_failure()
        assert self.cb.is_globally_open
        allowed, reason = self.cb.can_trade()
        assert not allowed
        assert "consecutive_failures" in reason

    def test_success_resets_failure_count(self) -> None:
        self.cb.on_trade_failure()
        self.cb.on_trade_failure()
        self.cb.on_trade_success()
        self.cb.on_trade_failure()
        assert not self.cb.is_globally_open

    def test_high_latency_trips(self) -> None:
        self.cb.on_api_latency(15_000)
        assert self.cb.is_globally_open

    def test_normal_latency_ok(self) -> None:
        self.cb.on_api_latency(500)
        assert not self.cb.is_globally_open

    def test_stuck_items_trips(self) -> None:
        self.cb.on_stuck_items_count(10)
        assert self.cb.is_globally_open

    def test_collection_block_on_wide_spread(self) -> None:
        now = datetime.now(timezone.utc)
        snapshot = MarketSnapshot(
            collection="test",
            market=Market.MRKT,
            timestamp=now,
            floor_price=1_000_000_000,
            listings=[],
            buy_orders=[],
            recent_sales=[],
            data_age_seconds=0.0,
        )
        # spread_pct property returns 1.0 if no bids → > threshold
        self.cb.check_collection_health("test", snapshot)
        assert self.cb.is_collection_blocked("test")

    def test_unblocked_collection_allows_trade(self) -> None:
        allowed, reason = self.cb.can_trade("some_collection")
        assert allowed

    def test_latency_trimming(self) -> None:
        """Latency list shouldn't grow unbounded."""
        for i in range(200):
            self.cb.on_api_latency(100)
        assert len(self.cb._recent_latencies) <= 100
