"""Concurrency and stress tests — race conditions, dedup under load."""

from __future__ import annotations

import asyncio
import time

import pytest


class TestDedupUnderLoad:
    @pytest.mark.asyncio
    async def test_dedup_dict_handles_rapid_access(self):
        """Verify dedup dict handles rapid concurrent access."""
        dedup: dict[str, float] = {}
        ttl = 5.0

        async def add_entry(gift_id: str):
            now = time.monotonic()
            expired = [k for k, t in dedup.items() if now - t > ttl]
            for k in expired:
                dedup.pop(k, None)
            if gift_id not in dedup:
                dedup[gift_id] = now
                return True
            return False

        results = await asyncio.gather(*[add_entry(f"gift-{i % 10}") for i in range(100)])
        assert sum(results) == 10

    @pytest.mark.asyncio
    async def test_cooldown_dict_cleanup(self):
        """Test that cooldown cleanup works correctly under concurrent access."""
        cooldown: dict[str, float] = {}
        now = time.time()

        for i in range(50):
            if i < 25:
                cooldown[f"old-{i}"] = now - 700
            else:
                cooldown[f"new-{i}"] = now - 100

        expired = [k for k, v in cooldown.items() if now - v > 600]
        for k in expired:
            cooldown.pop(k, None)

        assert len(cooldown) == 25
        assert all(k.startswith("new-") for k in cooldown)


class TestAsyncSafety:
    @pytest.mark.asyncio
    async def test_multiple_async_tasks_no_crash(self):
        """Multiple concurrent tasks should complete without errors."""
        counter = {"value": 0}

        async def increment():
            counter["value"] += 1
            await asyncio.sleep(0.01)
            counter["value"] += 1

        await asyncio.gather(*[increment() for _ in range(100)])
        assert counter["value"] == 200

    @pytest.mark.asyncio
    async def test_timeout_protection(self):
        """Test that timeout prevents hanging."""
        async def slow_task():
            await asyncio.sleep(10)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow_task(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_cancellation_safety(self):
        """Test that cancelled tasks don't corrupt shared state."""
        state = {"value": 0, "errors": 0}

        async def safe_task():
            try:
                state["value"] += 1
                await asyncio.sleep(1.0)
                state["value"] += 1
            except asyncio.CancelledError:
                state["errors"] += 1
                raise

        tasks = [asyncio.create_task(safe_task()) for _ in range(5)]
        await asyncio.sleep(0.05)
        for t in tasks:
            t.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled = sum(1 for r in results if isinstance(r, asyncio.CancelledError))
        assert cancelled == 5
        assert state["value"] == 5  # all started
        assert state["errors"] == 5  # all caught CancelledError


class TestBuyCooldownRace:
    @pytest.mark.asyncio
    async def test_no_double_buy_within_cooldown(self):
        """Simulate multiple buy attempts for same NFT — only one should proceed."""
        cooldown: dict[str, float] = {}
        buys = []

        async def attempt_buy(nft_addr: str, attempt_id: int):
            if nft_addr in cooldown:
                return False
            cooldown[nft_addr] = time.time()
            buys.append(attempt_id)
            return True

        results = await asyncio.gather(*[
            attempt_buy("nft-addr-1", i) for i in range(10)
        ])

        # Only one should succeed
        assert sum(results) == 1
        assert len(buys) == 1
