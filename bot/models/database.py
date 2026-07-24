"""SQLAlchemy models — single source of truth for all persisted state.

Tables:
  deals            — every trade lifecycle
  audit_log        — every decision/event (normalized, no giant JSON)
  execution_metrics — latency tracking (signal / execution / inventory)
  price_snapshots  — periodic market snapshots (compressed)
  price_history    — per-sale price records for rolling window
  distributed_locks — PG-based lock fallback
  collections      — cached collection metadata
  bot_settings     — key-value runtime config
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bot.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
_engine_kwargs: dict = {"echo": False}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=600,
    )
engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ── Deals ──────────────────────────────────────────────────────────────


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Asset identification
    gift_id: Mapped[str] = mapped_column(String(256))
    collection_name: Mapped[str] = mapped_column(String(256))
    model_name: Mapped[str] = mapped_column(String(256), default="")
    backdrop_name: Mapped[str] = mapped_column(String(256), default="")
    symbol_name: Mapped[str] = mapped_column(String(256), default="")
    nft_address: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Strategy
    strategy: Mapped[str] = mapped_column(String(32))  # Strategy enum value
    buy_market: Mapped[str] = mapped_column(String(16), default="mrkt")
    sell_market: Mapped[str] = mapped_column(String(16), default="getgems")

    # State machine
    state: Mapped[str] = mapped_column(String(32), default="discovered")
    state_updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    state_history: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "soft" | "hard"

    # Prices
    buy_price: Mapped[int] = mapped_column(BigInteger)
    sell_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_sell_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Predictions (at decision time)
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_sell_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_sell_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_net_profit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confidence_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Actuals (filled post-trade)
    actual_sell_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actual_sell_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_net_profit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Repricing
    reprice_count: Mapped[int] = mapped_column(Integer, default=0)
    last_repriced_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Order reference (for MRKT_ORDER_ARB)
    order_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # Shadow mode
    is_shadow: Mapped[bool] = mapped_column(Boolean, default=False)

    # Timestamps
    detected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    bought_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    listed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sold_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Heartbeat for async jobs (zombie detection)
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# ── Audit Log ──────────────────────────────────────────────────────────


class AuditLog(Base):
    """Every decision point.  Normalized columns + small JSONB for extras."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_deal", "deal_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Normalized fields (fast queries, no JSON bloat)
    collection: Mapped[str | None] = mapped_column(String(256), nullable=True)
    price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fair_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Small extras (NOT giant snapshots)
    extra: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# ── Execution Metrics ──────────────────────────────────────────────────


class ExecutionMetric(Base):
    """Per-deal latency breakdown: signal / execution / inventory."""

    __tablename__ = "execution_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(Integer)

    # Signal latency (market event → scanner detection)
    signal_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Execution latency breakdown
    detection_to_validation_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_to_buy_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    buy_to_settlement_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Inventory latency
    transfer_to_listing_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    listing_to_sold_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Totals
    total_execution_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_pipeline_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Was the opportunity still alive when we tried to buy?
    opportunity_alive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Price Snapshots (compressed, referenced by ID) ─────────────────────


class PriceSnapshot(Base):
    """Periodic market snapshot — normalized columns, NOT giant JSONB."""

    __tablename__ = "price_snapshots"
    __table_args__ = (Index("ix_snapshot_coll_time", "collection", "recorded_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(String(256))
    market: Mapped[str] = mapped_column(String(16))

    # Normalized metrics
    floor_price: Mapped[int] = mapped_column(BigInteger, default=0)
    fair_value: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    best_bid: Mapped[int] = mapped_column(BigInteger, default=0)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sales_count_24h: Mapped[int] = mapped_column(Integer, default=0)
    listings_count: Mapped[int] = mapped_column(Integer, default=0)
    listings_near_floor: Mapped[int] = mapped_column(Integer, default=0)
    bid_depth: Mapped[int] = mapped_column(Integer, default=0)
    volatility_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wash_trade_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Price History (per-sale records) ───────────────────────────────────


class PriceRecord(Base):
    """Individual sale/listing event for rolling window analysis."""

    __tablename__ = "price_records"
    __table_args__ = (Index("ix_price_rec_coll", "collection", "recorded_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(String(256))
    model: Mapped[str] = mapped_column(String(256), default="")
    market: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(32))  # "sale", "listing", "delist"
    price: Mapped[int] = mapped_column(BigInteger)
    gift_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    seller_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    buyer_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    wash_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Distributed Locks (PG fallback) ───────────────────────────────────


class DistributedLock(Base):
    __tablename__ = "distributed_locks"

    lock_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    acquired_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


# ── Collections (cache) ───────────────────────────────────────────────


class Collection(Base):
    __tablename__ = "collections"

    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    floor_price_nanoton: Mapped[int] = mapped_column(BigInteger, default=0)
    prev_day_floor_nanoton: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    craftable: Mapped[bool] = mapped_column(Boolean, default=False)
    getgems_floor_nanoton: Mapped[int] = mapped_column(BigInteger, default=0)
    scan_tier: Mapped[str] = mapped_column(String(16), default="cold")
    last_opportunity_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Bot Settings ──────────────────────────────────────────────────────


class BotSettings(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ── Balance Snapshots ──────────────────────────────────────────────────


class BalanceSnapshot(Base):
    """Periodic balance recording for P&L tracking."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (Index("ix_balance_ts", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    wallet_balance: Mapped[int] = mapped_column(BigInteger, default=0)
    mrkt_balance: Mapped[int] = mapped_column(BigInteger, default=0)
    gifts_value: Mapped[int] = mapped_column(BigInteger, default=0)
    gifts_count: Mapped[int] = mapped_column(Integer, default=0)
    total_value: Mapped[int] = mapped_column(BigInteger, default=0)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)


# ── Manual Trades ─────────────────────────────────────────────────────


class ManualTrade(Base):
    """Tracks manual buys/sells detected by inventory monitoring."""

    __tablename__ = "manual_trades"
    __table_args__ = (Index("ix_manual_ts", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    gift_name: Mapped[str] = mapped_column(String(256))
    collection: Mapped[str] = mapped_column(String(256))
    action: Mapped[str] = mapped_column(String(16))  # "buy" | "sell"
    market: Mapped[str] = mapped_column(String(16))  # mrkt | getgems | fragment
    price_nanoton: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_nanoton: Mapped[int] = mapped_column(BigInteger, default=0)
    gift_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)


# ── Init ──────────────────────────────────────────────────────────────


async def init_db(drop_existing: bool = False) -> None:
    async with engine.begin() as conn:
        if drop_existing:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
