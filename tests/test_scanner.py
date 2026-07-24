"""Tests for AdaptiveScanner — tier management, priority scoring."""

from __future__ import annotations

import time

import pytest

from bot.models.types import ScanTier
from bot.orchestration.scanner import (
    TIER_INTERVALS,
    AdaptiveScanner,
    CollectionPriority,
)


class TestCollectionPriority:
    def test_default_tier_is_cold(self):
        p = CollectionPriority("TestGift")
        assert p.tier == ScanTier.COLD
        assert p.collection == "TestGift"

    def test_priority_score_zero_initially(self):
        p = CollectionPriority("TestGift")
        assert p.priority_score == 0.0

    def test_priority_score_with_opportunities(self):
        p = CollectionPriority("TestGift")
        p.opportunity_count_1h = 3
        p.liquidity_score = 0.5
        assert p.priority_score > 0.0

    def test_recency_bonus(self):
        p = CollectionPriority("TestGift")
        p.last_opportunity_at = time.monotonic()  # just now
        score_fresh = p.priority_score
        p2 = CollectionPriority("TestGift2")
        p2.last_opportunity_at = time.monotonic() - 1200  # 20 min ago
        score_old = p2.priority_score
        assert score_fresh >= score_old

    def test_interval_fast_for_high_priority(self):
        p = CollectionPriority("TestGift")
        p.tier = ScanTier.HOT
        p.opportunity_count_1h = 5
        p.liquidity_score = 2.0
        lo, _ = TIER_INTERVALS[ScanTier.HOT]
        assert p.interval == lo

    def test_interval_slow_for_low_priority(self):
        p = CollectionPriority("TestGift")
        p.tier = ScanTier.COLD
        _, hi = TIER_INTERVALS[ScanTier.COLD]
        assert p.interval == hi


class TestAdaptiveScanner:
    def test_init_collections(self):
        async def _fetch_mrkt(c, m):
            return None

        async def _fetch_gg(c, m):
            return None

        async def _on_opp(s1, s2):
            return []

        scanner = AdaptiveScanner(
            collections=["Gift1", "Gift2", "Gift3"],
            fetch_mrkt=_fetch_mrkt,
            fetch_getgems=_fetch_gg,
            on_opportunities=_on_opp,
        )
        assert len(scanner._priorities) == 3
        assert "Gift1" in scanner._priorities
        assert "Gift2" in scanner._priorities
        assert "Gift3" in scanner._priorities

    def test_market_toggles(self):
        async def _noop(c, m):
            return None

        async def _on_opp(s1, s2):
            return []

        scanner = AdaptiveScanner(
            collections=[],
            fetch_mrkt=_noop,
            fetch_getgems=_noop,
            on_opportunities=_on_opp,
        )
        assert scanner.is_market_enabled("mrkt") is True
        scanner.set_market_enabled("mrkt", False)
        assert scanner.is_market_enabled("mrkt") is False

    def test_shadow_mode_toggle(self):
        async def _noop(c, m):
            return None

        async def _on_opp(s1, s2):
            return []

        scanner = AdaptiveScanner(
            collections=[],
            fetch_mrkt=_noop,
            fetch_getgems=_noop,
            on_opportunities=_on_opp,
            shadow_mode=True,
        )
        assert scanner.shadow_mode is True
        scanner.shadow_mode = False
        assert scanner.shadow_mode is False

    def test_staggered_initial_scans(self):
        async def _noop(c, m):
            return None

        async def _on_opp(s1, s2):
            return []

        scanner = AdaptiveScanner(
            collections=["A", "B", "C"],
            fetch_mrkt=_noop,
            fetch_getgems=_noop,
            on_opportunities=_on_opp,
        )
        times = [p.next_scan_at for p in scanner._priorities.values()]
        # Each staggered by 1.0s
        assert times[1] - times[0] == pytest.approx(1.0, abs=0.01)
        assert times[2] - times[1] == pytest.approx(1.0, abs=0.01)
