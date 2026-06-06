"""Exception hierarchy for the trading system.

Structured exception classes enable:
  - Precise error handling per failure type
  - Circuit breaker integration (hard vs soft failures)
  - Audit trail with typed error context
  - Retry decisions (transient vs permanent)
"""

from __future__ import annotations


class BotError(Exception):
    """Base exception for all trading bot errors."""


# ── API / Network ──────────────────────────────────────────────────────


class APIError(BotError):
    """Base for all API-related errors."""

    def __init__(self, message: str, status_code: int = 0, url: str = "") -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(message)


class RateLimitError(APIError):
    """HTTP 429 or equivalent rate limiting."""


class AuthenticationError(APIError):
    """HTTP 401/403 — token expired or invalid."""


class APITimeoutError(APIError):
    """Request timed out."""


# ── Trading ────────────────────────────────────────────────────────────


class TradingError(BotError):
    """Base for trading-related errors."""


class InsufficientBalanceError(TradingError):
    """Not enough funds to execute trade."""

    def __init__(self, message: str, required: int = 0, available: int = 0) -> None:
        self.required = required
        self.available = available
        super().__init__(message)


class DuplicatePurchaseError(TradingError):
    """Attempted to buy an already-owned or recently-bought gift."""

    def __init__(self, message: str, deal_id: str = "") -> None:
        self.deal_id = deal_id
        super().__init__(message)


class StaleDataError(TradingError):
    """Market data is too old to act on safely."""


class PricingError(TradingError):
    """Invalid or suspicious price detected."""


class OrderExpiredError(TradingError):
    """Opportunity deadline passed before execution."""


# ── State Machine ──────────────────────────────────────────────────────


class StateError(BotError):
    """Invalid state transition or state corruption."""


class LockError(BotError):
    """Failed to acquire or extend a distributed lock."""


# ── Configuration ──────────────────────────────────────────────────────


class ConfigError(BotError):
    """Missing or invalid configuration."""
