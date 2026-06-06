"""Audit trail — every decision point logged.

Event types:
  opportunity_found   — market snapshot at discovery
  scored              — opportunity score + rejection reasons
  pre_validation      — fresh checks before buy
  buy_executed        — buy result + latency
  state_transition    — from → to with reason
  repricing           — old → new price with context
  circuit_breaker     — trigger details
  post_trade          — expected vs realized analysis
  reconciliation      — post-buy ownership verification
  system              — startup, shutdown, errors

All entries use normalized columns for fast queries.
Small JSONB `extra` field for non-standard data only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bot.models.database import AuditLog, ExecutionMetric, async_session

logger = logging.getLogger(__name__)


async def log_event(
    event_type: str,
    *,
    deal_id: int | None = None,
    collection: str | None = None,
    price: int | None = None,
    fair_value: int | None = None,
    confidence: float | None = None,
    roi_pct: float | None = None,
    score: float | None = None,
    latency_ms: int | None = None,
    reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Insert one audit entry. Non-blocking — errors are logged, not raised."""
    try:
        async with async_session() as session:
            async with session.begin():
                entry = AuditLog(
                    deal_id=deal_id,
                    event_type=event_type,
                    timestamp=datetime.now(timezone.utc),
                    collection=collection,
                    price=price,
                    fair_value=fair_value,
                    confidence=confidence,
                    roi_pct=roi_pct,
                    score=score,
                    latency_ms=latency_ms,
                    reason=reason,
                    extra=extra,
                )
                session.add(entry)
    except Exception:
        logger.exception("Failed to write audit event=%s deal=%s", event_type, deal_id)


async def log_opportunity_found(
    deal_id: int,
    collection: str,
    buy_price: int,
    fair_value: int,
    confidence: float,
    roi_pct: float,
    score: float,
    strategy: str,
    sell_market: str,
) -> None:
    await log_event(
        "opportunity_found",
        deal_id=deal_id,
        collection=collection,
        price=buy_price,
        fair_value=fair_value,
        confidence=confidence,
        roi_pct=roi_pct,
        score=score,
        extra={"strategy": strategy, "sell_market": sell_market},
    )


async def log_scored(
    deal_id: int,
    score: float,
    passes: bool,
    rejection_reasons: list[str],
) -> None:
    await log_event(
        "scored",
        deal_id=deal_id,
        score=score,
        reason="; ".join(rejection_reasons) if rejection_reasons else "passed",
        extra={"passes": passes},
    )


async def log_pre_validation(
    deal_id: int,
    collection: str,
    price: int,
    fair_value: int,
    confidence: float,
    roi_pct: float,
    passed: bool,
    checks: dict[str, bool],
    latency_ms: int,
) -> None:
    await log_event(
        "pre_validation",
        deal_id=deal_id,
        collection=collection,
        price=price,
        fair_value=fair_value,
        confidence=confidence,
        roi_pct=roi_pct,
        latency_ms=latency_ms,
        reason="passed" if passed else "failed",
        extra={"checks": checks},
    )


async def log_buy_executed(
    deal_id: int,
    collection: str,
    price: int,
    success: bool,
    latency_ms: int,
    error: str | None = None,
) -> None:
    await log_event(
        "buy_executed",
        deal_id=deal_id,
        collection=collection,
        price=price,
        latency_ms=latency_ms,
        reason="success" if success else f"failed:{error}",
    )


async def log_state_transition(
    deal_id: int,
    from_state: str,
    to_state: str,
    reason: str,
) -> None:
    await log_event(
        "state_transition",
        deal_id=deal_id,
        reason=f"{from_state}→{to_state}: {reason}",
    )


async def log_repricing(
    deal_id: int,
    collection: str,
    old_price: int,
    new_price: int,
    reason: str,
) -> None:
    await log_event(
        "repricing",
        deal_id=deal_id,
        collection=collection,
        price=new_price,
        reason=reason,
        extra={"old_price": old_price},
    )


async def log_circuit_breaker(
    reason: str,
    is_open: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    await log_event(
        "circuit_breaker",
        reason=f"{'OPEN' if is_open else 'CLOSED'}: {reason}",
        extra=extra,
    )


async def log_reconciliation(
    deal_id: int,
    success: bool,
    details: dict[str, Any],
) -> None:
    await log_event(
        "reconciliation",
        deal_id=deal_id,
        reason="verified" if success else "mismatch",
        extra=details,
    )


async def log_system(event: str, extra: dict[str, Any] | None = None) -> None:
    await log_event("system", reason=event, extra=extra)


# ── Execution Metrics ──────────────────────────────────────────────────


async def save_execution_metrics(
    deal_id: int,
    *,
    signal_latency_ms: int | None = None,
    detection_to_validation_ms: int | None = None,
    validation_to_buy_ms: int | None = None,
    buy_to_settlement_ms: int | None = None,
    transfer_to_listing_ms: int | None = None,
    listing_to_sold_ms: int | None = None,
    total_execution_ms: int | None = None,
    total_pipeline_ms: int | None = None,
    opportunity_alive: bool | None = None,
) -> None:
    """Persist latency breakdown for a deal."""
    try:
        async with async_session() as session:
            async with session.begin():
                metric = ExecutionMetric(
                    deal_id=deal_id,
                    signal_latency_ms=signal_latency_ms,
                    detection_to_validation_ms=detection_to_validation_ms,
                    validation_to_buy_ms=validation_to_buy_ms,
                    buy_to_settlement_ms=buy_to_settlement_ms,
                    transfer_to_listing_ms=transfer_to_listing_ms,
                    listing_to_sold_ms=listing_to_sold_ms,
                    total_execution_ms=total_execution_ms,
                    total_pipeline_ms=total_pipeline_ms,
                    opportunity_alive=opportunity_alive,
                )
                session.add(metric)
    except Exception:
        logger.exception("Failed to save execution metrics deal=%d", deal_id)
