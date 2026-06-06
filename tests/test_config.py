"""Tests for config validation — ensures environment safety."""

from __future__ import annotations

from bot.config import NANOTON, settings


class TestConfigConstants:
    def test_nanoton_constant(self) -> None:
        assert NANOTON == 1_000_000_000

    def test_shadow_mode_default(self) -> None:
        """Shadow mode should default to True for safety."""
        assert settings.shadow_mode is True

    def test_fee_rates_reasonable(self) -> None:
        """Fees should be in 0-100% range."""
        assert 0 <= settings.mrkt_sell_fee_pct <= 100
        assert 0 <= settings.mrkt_buy_fee_pct <= 100
        assert 0 <= settings.getgems_sell_fee_pct <= 100

    def test_pool_size_positive(self) -> None:
        assert settings.db_pool_size >= 1
        assert settings.db_max_overflow >= 0
