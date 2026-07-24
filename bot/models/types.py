"""Shared types for the trading system.

All dataclasses, enums, and type aliases used across modules.
No business logic here — pure data definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# ── Enums ──────────────────────────────────────────────────────────────


class DealState(str, Enum):
    DISCOVERED = "discovered"
    VALIDATING = "validating"
    BUYING = "buying"
    BOUGHT = "bought"
    WITHDRAWING = "withdrawing"
    WITHDRAWN = "withdrawn"
    AWAITING_TRANSFER = "awaiting_transfer"
    TRANSFER_CONFIRMED = "transfer_confirmed"
    MONITORING_GETGEMS = "monitoring_getgems"
    LISTING = "listing"
    LISTED = "listed"
    RELISTING = "relisting"
    SOLD = "sold"
    SOFT_FAILED = "soft_failed"
    HARD_FAILED = "hard_failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[DealState] = frozenset(
    {DealState.SOLD, DealState.SOFT_FAILED, DealState.HARD_FAILED, DealState.CANCELLED}
)

VALID_TRANSITIONS: dict[DealState, frozenset[DealState]] = {
    DealState.DISCOVERED: frozenset({DealState.VALIDATING, DealState.CANCELLED}),
    DealState.VALIDATING: frozenset({DealState.BUYING, DealState.CANCELLED, DealState.SOFT_FAILED}),
    DealState.BUYING: frozenset({DealState.BOUGHT, DealState.HARD_FAILED, DealState.SOFT_FAILED}),
    DealState.BOUGHT: frozenset({DealState.WITHDRAWING, DealState.LISTING, DealState.HARD_FAILED}),
    DealState.WITHDRAWING: frozenset({DealState.WITHDRAWN, DealState.HARD_FAILED}),
    DealState.WITHDRAWN: frozenset({DealState.AWAITING_TRANSFER}),
    DealState.AWAITING_TRANSFER: frozenset({DealState.TRANSFER_CONFIRMED, DealState.CANCELLED}),
    DealState.TRANSFER_CONFIRMED: frozenset({DealState.MONITORING_GETGEMS}),
    DealState.MONITORING_GETGEMS: frozenset({DealState.LISTING, DealState.SOFT_FAILED}),
    DealState.LISTING: frozenset({DealState.LISTED, DealState.HARD_FAILED}),
    DealState.LISTED: frozenset({DealState.SOLD, DealState.RELISTING, DealState.CANCELLED}),
    DealState.RELISTING: frozenset({DealState.LISTED, DealState.CANCELLED}),
}


class Market(str, Enum):
    MRKT = "mrkt"
    GETGEMS = "getgems"
    FRAGMENT = "fragment"
    PORTAL = "portal"


class Strategy(str, Enum):
    CROSS_MARKET = "cross_market"
    MRKT_ORDER_ARB = "mrkt_order_arb"
    DEEP_DISCOUNT = "deep_discount"


class FailureType(str, Enum):
    """Soft failures don't degrade system health metrics."""

    SOFT = "soft"  # stale data, order disappeared, pnl degraded
    HARD = "hard"  # wallet error, API inconsistency, corrupted state


class ScanTier(str, Enum):
    HOT = "hot"  # 2-5 sec
    WARM = "warm"  # 10-30 sec
    COLD = "cold"  # 60-120 sec


# ── Market Data ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Listing:
    gift_id: str
    collection: str
    model: str
    symbol: str
    backdrop: str
    price: int  # nanoTON
    seller_id: str
    listed_at: datetime | None
    market: Market
    number: int | None = None


@dataclass(frozen=True, slots=True)
class Sale:
    gift_id: str
    collection: str
    price: int  # nanoTON
    buyer_id: str
    seller_id: str
    sold_at: datetime
    market: Market


@dataclass(frozen=True, slots=True)
class Order:
    order_id: str
    collection: str
    model: str | None
    symbol: str | None
    backdrop: str | None
    price_min: int
    price_max: int
    quantity_total: int
    quantity_filled: int
    creator_id: str
    created_at: datetime | None


@dataclass(slots=True)
class MarketSnapshot:
    """Point-in-time view of a collection's market on one marketplace."""

    collection: str
    market: Market
    timestamp: datetime
    listings: list[Listing]
    recent_sales: list[Sale]
    buy_orders: list[Order]
    floor_price: int
    data_age_seconds: float
    getgems_floor: int = 0

    @property
    def is_fresh(self) -> bool:
        return self.data_age_seconds <= 5.0

    @property
    def best_bid(self) -> int:
        if not self.buy_orders:
            return 0
        return max(o.price_max for o in self.buy_orders)

    @property
    def spread_pct(self) -> float:
        if self.floor_price <= 0:
            return 1.0
        bid = self.best_bid
        if bid <= 0:
            return 1.0
        return (self.floor_price - bid) / self.floor_price


# ── Pricing ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PricingResult:
    fair_value: int
    confidence: float  # 0.0–1.0 (8-factor composite)
    liquidity_score: float  # 0.0–1.0
    sell_probability_24h: float
    expected_sell_hours: float
    spread_pct: float
    listings_near_floor: int  # within 5% of floor
    sales_velocity_24h: float
    rolling_median: int | None
    volatility_pct: float  # stddev / mean
    floor_churn_rate: float
    orderbook_imbalance: float  # (bid_vol - ask_vol) / total
    wash_trade_score: float  # 0=clean, 1=likely wash
    confidence_factors: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ScoredSale:
    """A sale annotated with wash-trade probability."""

    sale: Sale
    wash_score: float  # 0.0–1.0


# ── PnL ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PnLResult:
    gross_profit: int
    net_profit: int
    roi_pct: float
    annualized_roi_pct: float
    capital_efficiency: float  # net_profit / (cost × hold_days)
    total_cost: int
    fees: int
    gas: int
    slippage_buffer: int
    decay_estimate: int
    relisting_risk: int
    opportunity_cost: int
    expected_hold_hours: float


# ── Opportunity Scoring ────────────────────────────────────────────────


@dataclass(slots=True)
class OpportunityScore:
    raw_score: float
    expected_profit: int
    confidence: float
    liquidity_score: float
    fill_probability: float
    expected_hold_hours: float
    passes_threshold: bool
    rejection_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Opportunity:
    """A detected potential trade before execution."""

    gift_id: str
    collection: str
    model: str
    symbol: str
    backdrop: str
    listing_price: int
    strategy: Strategy
    buy_market: Market
    sell_market: Market
    target_sell_price: int
    order_id: str | None  # for MRKT_ORDER_ARB
    pnl: PnLResult | None
    score: OpportunityScore | None
    detected_at: datetime
    expires_at: datetime  # hard deadline — abort if pipeline exceeds this
    snapshot_id: int | None  # reference to stored snapshot

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at


# ── Execution Metrics ──────────────────────────────────────────────────


@dataclass(slots=True)
class LatencyMetrics:
    """Three distinct latency types — never mix them."""

    # Signal latency: market event → scanner detection
    signal_latency_ms: int | None = None

    # Execution latency: detection → order fill
    detection_ts: datetime | None = None
    validation_start_ts: datetime | None = None
    validation_end_ts: datetime | None = None
    buy_start_ts: datetime | None = None
    buy_end_ts: datetime | None = None

    # Inventory latency: fill → liquidation
    settlement_ts: datetime | None = None
    listing_ts: datetime | None = None
    sold_ts: datetime | None = None

    @property
    def execution_latency_ms(self) -> int | None:
        if self.buy_end_ts and self.detection_ts:
            return int((self.buy_end_ts - self.detection_ts).total_seconds() * 1000)
        return None

    @property
    def inventory_latency_ms(self) -> int | None:
        if self.sold_ts and self.settlement_ts:
            return int((self.sold_ts - self.settlement_ts).total_seconds() * 1000)
        return None

    @property
    def total_pipeline_ms(self) -> int | None:
        if self.sold_ts and self.detection_ts:
            return int((self.sold_ts - self.detection_ts).total_seconds() * 1000)
        return None


# ── Circuit Breaker ────────────────────────────────────────────────────


@dataclass(slots=True)
class CircuitState:
    is_open: bool = False
    reason: str = ""
    opened_at: datetime | None = None
    cooldown_seconds: int = 300
    consecutive_failures: int = 0
    api_latency_p95_ms: float = 0
    volatility_spike: bool = False
    sales_collapse: bool = False


# ── Market Quality ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketQualityResult:
    is_tradeable: bool
    reason: str  # "ok" or rejection reason
    sales_velocity_24h: float
    spread_pct: float
    listings_count: int
    bid_depth: int
    wash_trade_score: float


# ── Freshness ──────────────────────────────────────────────────────────


FRESHNESS_SNAPSHOT_MAX_SEC: float = 2.0
FRESHNESS_ORDER_MAX_SEC: float = 1.0
FRESHNESS_LISTING_MAX_SEC: float = 1.0

# Execution deadlines (seconds from detection) — gifts don't move fast
DEADLINE_HOT_SEC: float = 60.0
DEADLINE_WARM_SEC: float = 120.0
DEADLINE_COLD_SEC: float = 300.0


# ── Post-Trade ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PostTradeReport:
    deal_id: int
    pricing_error_pct: float
    time_prediction_error_pct: float
    realized_roi: float
    predicted_roi: float
    pnl_divergence: float  # expected - realized (key metric)


# ── Helpers ────────────────────────────────────────────────────────────


NANOTON = 1_000_000_000


def nanoton_to_ton(n: int) -> float:
    return n / NANOTON


def ton_to_nanoton(t: float) -> int:
    return int(t * NANOTON)
