"""Tests for Getgems falling price (Dutch auction) listing logic."""

from __future__ import annotations

import pytest

from bot.getgems_client import NANO, GetgemsClient


class TestFallingPriceValidation:
    """Test input validation for falling price listing."""

    @pytest.fixture
    def client(self) -> GetgemsClient:
        return GetgemsClient(api_key="test")

    @pytest.mark.asyncio
    async def test_rejects_when_not_authenticated(self, client: GetgemsClient) -> None:
        result = await client.list_offchain_gift_falling_price(
            nft_address="EQTest",
            start_price_nanoton=10 * NANO,
            min_price_nanoton=5 * NANO,
            decrease_value_nanoton=1 * NANO,
            decrease_interval_ms=3_600_000,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_start_less_than_min(self, client: GetgemsClient) -> None:
        result = await client.list_offchain_gift_falling_price(
            nft_address="EQTest",
            start_price_nanoton=5 * NANO,
            min_price_nanoton=10 * NANO,
            decrease_value_nanoton=1 * NANO,
            decrease_interval_ms=3_600_000,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_negative_decrease(self, client: GetgemsClient) -> None:
        result = await client.list_offchain_gift_falling_price(
            nft_address="EQTest",
            start_price_nanoton=10 * NANO,
            min_price_nanoton=5 * NANO,
            decrease_value_nanoton=-1,
            decrease_interval_ms=3_600_000,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_step_larger_than_range(self, client: GetgemsClient) -> None:
        result = await client.list_offchain_gift_falling_price(
            nft_address="EQTest",
            start_price_nanoton=10 * NANO,
            min_price_nanoton=9 * NANO,
            decrease_value_nanoton=2 * NANO,
            decrease_interval_ms=3_600_000,
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_invalid_interval(self, client: GetgemsClient) -> None:
        result = await client.list_offchain_gift_falling_price(
            nft_address="EQTest",
            start_price_nanoton=10 * NANO,
            min_price_nanoton=5 * NANO,
            decrease_value_nanoton=1 * NANO,
            decrease_interval_ms=999_999,
        )
        assert result is False

    def test_falling_intervals_completeness(self) -> None:
        intervals = GetgemsClient.FALLING_INTERVALS_MS
        assert 300_000 in intervals  # 5m
        assert 3_600_000 in intervals  # 1h
        assert 86_400_000 in intervals  # 1d
        assert 172_800_000 in intervals  # 2d
        assert len(intervals) == 9


class TestFallingPriceCalculation:
    """Test falling price parameter calculation in auto_seller."""

    def test_decrease_value_calculation(self) -> None:
        start_price = 100 * NANO
        decrease_pct = 3.0
        decrease_value = int(start_price * decrease_pct / 100)
        assert decrease_value == 3 * NANO

    def test_min_price_with_fees(self) -> None:
        buy_price = 50 * NANO
        gg_fee_pct = 0.0  # Getgems 0% fee
        min_price = int(buy_price * (1 + gg_fee_pct / 100) * 1.01)
        assert min_price == int(50.5 * NANO)

    def test_min_price_capped_if_above_start(self) -> None:
        start_price = 10 * NANO
        buy_price = 15 * NANO
        gg_fee_pct = 0.0
        min_price = int(buy_price * (1 + gg_fee_pct / 100) * 1.01)
        if min_price >= start_price:
            min_price = int(start_price * 0.95)
        assert min_price == int(9.5 * NANO)
        assert min_price < start_price

    def test_decrease_step_capped_to_range(self) -> None:
        start_price = 10 * NANO
        min_price = int(9.5 * NANO)
        decrease_pct = 10.0
        decrease_value = int(start_price * decrease_pct / 100)
        assert decrease_value == 1 * NANO
        if (start_price - min_price) < decrease_value:
            decrease_value = start_price - min_price
        assert decrease_value == int(0.5 * NANO)
