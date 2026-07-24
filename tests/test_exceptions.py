"""Tests for exception hierarchy — typed error handling."""

from __future__ import annotations

import pytest

from bot.core.exceptions import (
    APIError,
    AuthenticationError,
    BotError,
    DuplicatePurchaseError,
    InsufficientBalanceError,
    PricingError,
    RateLimitError,
    StaleDataError,
    TradingError,
)


class TestExceptionHierarchy:
    def test_all_inherit_from_bot_error(self) -> None:
        errors = [
            APIError("test"),
            RateLimitError("test"),
            AuthenticationError("test"),
            TradingError("test"),
            InsufficientBalanceError("test", 100, 50),
            DuplicatePurchaseError("test", "deal1"),
            StaleDataError("test"),
            PricingError("test"),
        ]
        for err in errors:
            assert isinstance(err, BotError)

    def test_api_error_attributes(self) -> None:
        err = APIError("timeout", status_code=429, url="/api/v1")
        assert err.status_code == 429
        assert err.url == "/api/v1"
        assert "timeout" in str(err)

    def test_rate_limit_is_api_error(self) -> None:
        err = RateLimitError("throttled")
        assert isinstance(err, APIError)

    def test_insufficient_balance_attrs(self) -> None:
        err = InsufficientBalanceError("low", required=100, available=50)
        assert err.required == 100
        assert err.available == 50

    def test_duplicate_purchase_attrs(self) -> None:
        err = DuplicatePurchaseError("dupe", deal_id="deal123")
        assert err.deal_id == "deal123"

    def test_can_catch_by_parent(self) -> None:
        """Catching APIError should also catch RateLimitError."""
        with pytest.raises(APIError):
            raise RateLimitError("rate limited")
