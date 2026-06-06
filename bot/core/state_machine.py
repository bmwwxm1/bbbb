"""Persistent finite state machine for deal lifecycle.

Properties:
- Atomic transitions via UPDATE ... WHERE state = :from (optimistic locking)
- State history persisted in JSONB column
- Idempotency keys prevent double execution
- Recovery on restart: scan non-terminal deals, resume or timeout
- Retry tracking: max retries per state, then HARD_FAILED
- Heartbeat: detect zombie deals (async jobs that died)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update

from bot.models.database import Deal, async_session
from bot.models.types import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    DealState,
    FailureType,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
ZOMBIE_THRESHOLD_MINUTES = 15  # no heartbeat for this long → zombie


async def transition(
    deal_id: int,
    from_state: DealState,
    to_state: DealState,
    reason: str = "",
    error: str | None = None,
    failure_type: FailureType | None = None,
) -> bool:
    """Atomic state transition. Returns False if current state != from_state.

    This is the ONLY way to change deal state. All transitions are:
    - Explicit (caller declares expected from_state)
    - Auditable (appended to state_history)
    - Validated (only allowed transitions succeed)
    """
    if to_state not in VALID_TRANSITIONS.get(from_state, frozenset()):
        logger.error("Invalid transition deal=%d: %s → %s", deal_id, from_state, to_state)
        return False

    now = datetime.now(timezone.utc)
    history_entry = {
        "from": from_state.value,
        "to": to_state.value,
        "reason": reason,
        "at": now.isoformat(),
    }
    if error:
        history_entry["error"] = error[:500]

    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                update(Deal)
                .where(Deal.id == deal_id, Deal.state == from_state.value)
                .values(
                    state=to_state.value,
                    state_updated_at=now,
                    last_error=error[:1000] if error else None,
                    failure_type=failure_type.value if failure_type else None,
                )
                .returning(Deal.id)
            )
            row = result.fetchone()
            if row is None:
                logger.warning(
                    "Transition failed (race): deal=%d expected=%s target=%s",
                    deal_id,
                    from_state,
                    to_state,
                )
                return False

            # Append to state_history safely (SQLite JSON concat is unreliable)
            result_hist = await session.execute(
                select(Deal.state_history).where(Deal.id == deal_id)
            )
            current_history = result_hist.scalar() or []
            if not isinstance(current_history, list):
                current_history = []
            current_history.append(history_entry)
            await session.execute(
                update(Deal).where(Deal.id == deal_id).values(state_history=current_history)
            )

    logger.info("Transition deal=%d: %s → %s (%s)", deal_id, from_state, to_state, reason)
    return True


async def soft_fail(deal_id: int, from_state: DealState, reason: str) -> bool:
    """Transition to SOFT_FAILED. Does NOT degrade system health metrics."""
    return await transition(
        deal_id,
        from_state,
        DealState.SOFT_FAILED,
        reason=reason,
        error=reason,
        failure_type=FailureType.SOFT,
    )


async def hard_fail(deal_id: int, from_state: DealState, reason: str) -> bool:
    """Transition to HARD_FAILED. Counts toward system health degradation."""
    return await transition(
        deal_id,
        from_state,
        DealState.HARD_FAILED,
        reason=reason,
        error=reason,
        failure_type=FailureType.HARD,
    )


async def increment_retry(deal_id: int) -> int:
    """Increment retry count. Returns new count."""
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                update(Deal)
                .where(Deal.id == deal_id)
                .values(retry_count=Deal.retry_count + 1)
                .returning(Deal.retry_count)
            )
            row = result.fetchone()
            return row[0] if row else 0


async def update_heartbeat(deal_id: int) -> None:
    """Update heartbeat timestamp for zombie detection."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        async with session.begin():
            await session.execute(update(Deal).where(Deal.id == deal_id).values(heartbeat_at=now))


async def create_deal(
    gift_id: str,
    collection_name: str,
    model_name: str,
    backdrop_name: str,
    symbol_name: str,
    buy_price: int,
    strategy: str,
    buy_market: str,
    sell_market: str,
    idempotency_key: str,
    is_shadow: bool = False,
    target_sell_price: int | None = None,
    expected_sell_price: int | None = None,
    expected_sell_hours: float | None = None,
    expected_roi: float | None = None,
    expected_net_profit: int | None = None,
    confidence_at_entry: float | None = None,
    opportunity_score: float | None = None,
    snapshot_id: int | None = None,
    order_id: str | None = None,
) -> int | None:
    """Create a new deal in DISCOVERED state. Returns deal_id or None if duplicate."""
    async with async_session() as session:
        async with session.begin():
            deal = Deal(
                gift_id=gift_id,
                collection_name=collection_name,
                model_name=model_name,
                backdrop_name=backdrop_name,
                symbol_name=symbol_name,
                buy_price=buy_price,
                strategy=strategy,
                buy_market=buy_market,
                sell_market=sell_market,
                state=DealState.DISCOVERED.value,
                state_updated_at=datetime.now(timezone.utc),
                state_history=[],
                idempotency_key=idempotency_key,
                is_shadow=is_shadow,
                target_sell_price=target_sell_price,
                expected_sell_price=expected_sell_price,
                expected_sell_hours=expected_sell_hours,
                expected_roi=expected_roi,
                expected_net_profit=expected_net_profit,
                confidence_at_entry=confidence_at_entry,
                opportunity_score=opportunity_score,
                snapshot_id=snapshot_id,
                order_id=order_id,
            )
            session.add(deal)
            try:
                await session.flush()
                return deal.id
            except Exception:
                # Idempotency key collision — duplicate
                logger.debug("Duplicate deal: %s", idempotency_key)
                return None


async def get_deal(deal_id: int) -> Deal | None:
    async with async_session() as session:
        result = await session.execute(select(Deal).where(Deal.id == deal_id))
        return result.scalar_one_or_none()


async def get_active_deals() -> list[Deal]:
    """All deals NOT in terminal states."""
    async with async_session() as session:
        terminal = [s.value for s in TERMINAL_STATES]
        result = await session.execute(select(Deal).where(Deal.state.notin_(terminal)))
        return list(result.scalars().all())


async def get_deals_by_state(state: DealState) -> list[Deal]:
    async with async_session() as session:
        result = await session.execute(select(Deal).where(Deal.state == state.value))
        return list(result.scalars().all())


async def get_zombie_deals() -> list[Deal]:
    """Deals with stale heartbeats in non-terminal async states."""
    threshold = datetime.now(timezone.utc) - timedelta(minutes=ZOMBIE_THRESHOLD_MINUTES)
    async_states = [
        DealState.MONITORING_GETGEMS.value,
        DealState.BUYING.value,
        DealState.WITHDRAWING.value,
        DealState.LISTING.value,
    ]
    async with async_session() as session:
        result = await session.execute(
            select(Deal).where(
                Deal.state.in_(async_states),
                Deal.heartbeat_at < threshold,
            )
        )
        return list(result.scalars().all())


async def recover_on_startup() -> dict[str, Any]:
    """Scan non-terminal deals and recover or fail them.

    Returns summary of actions taken.
    """
    summary: dict[str, list[int]] = {"recovered": [], "timed_out": [], "zombies": []}

    try:
        active = await get_active_deals()
    except Exception as e:
        logger.warning("Startup recovery: failed to load deals: %s — clearing stale data", e)
        # Force all non-terminal deals to SOFT_FAILED to unblock
        try:
            async with async_session() as session:
                terminal = [s.value for s in TERMINAL_STATES]
                await session.execute(
                    update(Deal)
                    .where(Deal.state.notin_(terminal))
                    .values(
                        state=DealState.SOFT_FAILED.value,
                        last_error="recovery_cleanup_after_json_error",
                    )
                )
                await session.commit()
        except Exception as e2:
            logger.error("Startup recovery: cleanup also failed: %s", e2)
        return summary

    now = datetime.now(timezone.utc)

    for deal in active:
        state = DealState(deal.state)
        updated_at = deal.state_updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_minutes = (now - updated_at).total_seconds() / 60

        # Zombie detection
        if state in (
            DealState.BUYING,
            DealState.WITHDRAWING,
            DealState.LISTING,
        ):
            if age_minutes > ZOMBIE_THRESHOLD_MINUTES:
                await hard_fail(deal.id, state, f"zombie_timeout:{age_minutes:.0f}min")
                summary["zombies"].append(deal.id)
                continue

        # MONITORING_GETGEMS can legitimately take 30+ min — restart monitoring
        if state == DealState.MONITORING_GETGEMS:
            if age_minutes > 60:
                await soft_fail(deal.id, state, "monitoring_timeout:60min")
                summary["timed_out"].append(deal.id)
            else:
                summary["recovered"].append(deal.id)
            continue

        # VALIDATING that got stuck
        if state == DealState.VALIDATING and age_minutes > 2:
            await soft_fail(deal.id, state, "validation_timeout")
            summary["timed_out"].append(deal.id)
            continue

    logger.info(
        "Startup recovery: recovered=%d timed_out=%d zombies=%d",
        len(summary["recovered"]),
        len(summary["timed_out"]),
        len(summary["zombies"]),
    )
    return summary
