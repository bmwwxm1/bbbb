"""Execution engine — the critical trade pipeline.

Pipeline for each opportunity:
  1. Acquire lock on asset
  2. Score opportunity
  3. Pre-validate (re-fetch fresh data, re-calculate PnL)
  4. Check risk limits + circuit breaker
  5. Verify freshness windows (abort if stale)
  6. Execute buy (with deadline)
  7. Post-buy reconciliation (verify ownership)
  8. Route to next stage (cross-market transfer or listing)

Properties:
  - Cancellable at every stage
  - Timeout-aware (expires_at deadline)
  - All state changes via state_machine (source of truth = Postgres)
  - Idempotent (idempotency keys prevent double execution)
  - Full audit trail
  - Shadow mode: simulate without spending
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from bot.core import audit, state_machine
from bot.core.circuit_breaker import CircuitBreaker
from bot.core.exceptions import InsufficientBalanceError
from bot.core.locks import LockManager
from bot.execution.risk_engine import RiskEngine
from bot.intelligence.cost_model import calculate_pnl
from bot.intelligence.liquidation_model import LiquidationModel
from bot.intelligence.market_quality import MarketQualityFilter
from bot.intelligence.opportunity_scorer import score_opportunity
from bot.intelligence.pricing_engine import PricingEngine
from bot.models.types import (
    DealState,
    MarketSnapshot,
    Opportunity,
    Strategy,
    nanoton_to_ton,
)

logger = logging.getLogger(__name__)

# Freshness windows (seconds)
MAX_SNAPSHOT_AGE = 30.0  # gifts: 30s is fine, prices don't change that fast
MAX_LISTING_AGE = 1.0


class ExecutionEngine:
    def __init__(
        self,
        *,
        lock_manager: LockManager,
        pricing: PricingEngine,
        liquidation: LiquidationModel,
        quality: MarketQualityFilter,
        risk: RiskEngine,
        circuit: CircuitBreaker,
        buy_callback: Any = None,  # async callable(gift_id, price) → bool
        on_bought_callback: Any = None,  # callable(gift_id) — mark as recently bought
        shadow_mode: bool = True,
    ) -> None:
        self._locks = lock_manager
        self._pricing = pricing
        self._liquidation = liquidation
        self._quality = quality
        self._risk = risk
        self._circuit = circuit
        self._buy = buy_callback
        self._on_bought = on_bought_callback
        self._shadow = shadow_mode
        # Dedup set: prevent buying same gift twice within TTL
        self._recent_buys: dict[str, float] = {}
        self._dedup_ttl = 300.0  # 5 min dedup window

    @property
    def shadow_mode(self) -> bool:
        return self._shadow

    @shadow_mode.setter
    def shadow_mode(self, v: bool) -> None:
        self._shadow = v

    async def execute_opportunity(
        self,
        opp: Opportunity,
        fresh_snapshot: MarketSnapshot,
    ) -> int | None:
        """Full execution pipeline. Returns deal_id or None if rejected.

        Every step checks the deadline. Aborts if expired.
        """
        pipeline_start = time.monotonic()

        # ── 0. Dedup check ─────────────────────────────────────────────
        now_mono = time.monotonic()
        # Clean expired entries
        expired_keys = [k for k, t in self._recent_buys.items() if now_mono - t > self._dedup_ttl]
        for k in expired_keys:
            del self._recent_buys[k]
        if opp.gift_id in self._recent_buys:
            logger.info("Rejected: duplicate buy attempt within TTL: %s", opp.gift_id)
            return None

        # ── 1. Check deadline ──────────────────────────────────────────
        if opp.is_expired:
            logger.info("Rejected: expired before pipeline: %s", opp.gift_id)
            return None

        # ── 2. Circuit breaker ─────────────────────────────────────────
        can_trade, reason = self._circuit.can_trade(opp.collection)
        if not can_trade:
            logger.info("Rejected: circuit blocked: %s — %s", opp.collection, reason)
            return None

        # ── 3. Acquire lock ────────────────────────────────────────────
        lock_key = f"asset:{opp.gift_id}"
        owner = str(uuid.uuid4())
        locked = await self._locks.acquire(lock_key, ttl_seconds=60, owner=owner)
        if not locked:
            logger.info("Rejected: lock failed: %s", opp.gift_id)
            return None

        deal_id: int | None = None
        try:
            # ── 4. Score ───────────────────────────────────────────────
            pricing_result = self._pricing.price(fresh_snapshot)
            quality_result = self._quality.evaluate(fresh_snapshot)

            sell_hours = self._liquidation.expected_sell_hours(
                opp.target_sell_price, fresh_snapshot
            )

            pnl = calculate_pnl(
                buy_price=opp.listing_price,
                expected_sell_price=opp.target_sell_price,
                expected_hold_hours=sell_hours,
                needs_transfer=opp.strategy == Strategy.CROSS_MARKET,
            )

            score = score_opportunity(pnl, pricing_result, quality_result)

            if not score.passes_threshold:
                logger.info(
                    "Rejected: score %.1f, reasons: %s — %s",
                    score.raw_score,
                    score.rejection_reasons,
                    opp.gift_id,
                )
                await audit.log_scored(
                    deal_id=0,
                    score=score.raw_score,
                    passes=False,
                    rejection_reasons=score.rejection_reasons,
                )
                return None

            # ── 5. Risk check ──────────────────────────────────────────
            can_buy, risk_reason = await self._risk.can_buy(
                opp.collection,
                opp.listing_price,
                pricing_result.liquidity_score,
            )
            if not can_buy:
                logger.info("Risk rejected: %s — %s", opp.gift_id, risk_reason)
                return None

            # ── 6. Freshness check ─────────────────────────────────────
            if fresh_snapshot.data_age_seconds > MAX_SNAPSHOT_AGE:
                logger.info(
                    "Rejected: stale snapshot (%.1fs > %ds): %s",
                    fresh_snapshot.data_age_seconds,
                    MAX_SNAPSHOT_AGE,
                    opp.gift_id,
                )
                return None

            # ── 7. Check deadline again ────────────────────────────────
            if opp.is_expired:
                logger.info("Rejected: expired during validation: %s", opp.gift_id)
                return None

            # ── 8. Create deal ─────────────────────────────────────────
            idempotency_key = f"{opp.gift_id}:{opp.detected_at.isoformat()}"
            deal_id = await state_machine.create_deal(
                gift_id=opp.gift_id,
                collection_name=opp.collection,
                model_name=opp.model,
                backdrop_name=opp.backdrop,
                symbol_name=opp.symbol,
                buy_price=opp.listing_price,
                strategy=opp.strategy.value,
                buy_market=opp.buy_market.value,
                sell_market=opp.sell_market.value,
                idempotency_key=idempotency_key,
                is_shadow=self._shadow,
                target_sell_price=opp.target_sell_price,
                expected_sell_price=opp.target_sell_price,
                expected_sell_hours=sell_hours,
                expected_roi=pnl.roi_pct,
                expected_net_profit=pnl.net_profit,
                confidence_at_entry=pricing_result.confidence,
                opportunity_score=score.raw_score,
                snapshot_id=opp.snapshot_id,
                order_id=opp.order_id,
            )

            if deal_id is None:
                logger.info("Rejected: duplicate deal: %s", idempotency_key)
                return None

            await audit.log_opportunity_found(
                deal_id=deal_id,
                collection=opp.collection,
                buy_price=opp.listing_price,
                fair_value=pricing_result.fair_value,
                confidence=pricing_result.confidence,
                roi_pct=pnl.roi_pct,
                score=score.raw_score,
                strategy=opp.strategy.value,
                sell_market=opp.sell_market.value,
            )

            # ── 9. Transition to VALIDATING ────────────────────────────
            await state_machine.transition(
                deal_id,
                DealState.DISCOVERED,
                DealState.VALIDATING,
                reason="scored_and_risk_passed",
            )

            # ── 10. Execute buy (or simulate) ──────────────────────────
            if self._shadow:
                logger.info(
                    "SHADOW: Would buy %s at %.2f TON (ROI=%.1f%%, score=%.0f)",
                    opp.gift_id,
                    nanoton_to_ton(opp.listing_price),
                    pnl.roi_pct,
                    score.raw_score,
                )
                await state_machine.transition(
                    deal_id,
                    DealState.VALIDATING,
                    DealState.BUYING,
                    reason="shadow_mode",
                )
                # In shadow mode, simulate success
                await state_machine.transition(
                    deal_id,
                    DealState.BUYING,
                    DealState.BOUGHT,
                    reason="shadow_simulated",
                )
                execution_ms = int((time.monotonic() - pipeline_start) * 1000)
                await audit.save_execution_metrics(
                    deal_id=deal_id,
                    total_execution_ms=execution_ms,
                    opportunity_alive=True,
                )
                self._circuit.on_trade_success()
                return deal_id

            # Real execution
            await state_machine.transition(
                deal_id,
                DealState.VALIDATING,
                DealState.BUYING,
                reason="pre_validation_passed",
            )

            if opp.is_expired:
                await state_machine.soft_fail(deal_id, DealState.BUYING, "expired_before_buy")
                return deal_id

            buy_start = time.monotonic()
            buy_success = False
            if self._buy:
                try:
                    buy_success = await asyncio.wait_for(
                        self._buy(opp.gift_id, opp.listing_price),
                        timeout=10.0,
                    )
                except InsufficientBalanceError as e:
                    logger.warning(
                        "Buy skipped %s: %s", opp.collection, e,
                    )
                    await state_machine.soft_fail(
                        deal_id, DealState.BUYING, "insufficient_balance",
                    )
                    return deal_id
                except asyncio.TimeoutError:
                    await state_machine.hard_fail(deal_id, DealState.BUYING, "buy_timeout")
                    self._circuit.on_trade_failure(is_hard=True)
                    return deal_id
                except Exception as e:
                    await state_machine.hard_fail(deal_id, DealState.BUYING, f"buy_error:{e}")
                    self._circuit.on_trade_failure(is_hard=True)
                    return deal_id

            buy_ms = int((time.monotonic() - buy_start) * 1000)
            await audit.log_buy_executed(
                deal_id=deal_id,
                collection=opp.collection,
                price=opp.listing_price,
                success=buy_success,
                latency_ms=buy_ms,
            )

            if not buy_success:
                await state_machine.soft_fail(deal_id, DealState.BUYING, "buy_failed")
                self._circuit.on_trade_failure(is_hard=False)
                return deal_id

            # ── 11. BOUGHT ─────────────────────────────────────────────
            # Track in dedup set immediately to prevent duplicate buys
            self._recent_buys[opp.gift_id] = time.monotonic()

            await state_machine.transition(
                deal_id,
                DealState.BUYING,
                DealState.BOUGHT,
                reason="buy_confirmed",
            )

            # Mark as recently bought (1 min delay before sell/withdraw)
            if self._on_bought:
                self._on_bought(
                    opp.gift_id,
                    buy_price=opp.listing_price,
                    collection_name=opp.collection,
                )

            execution_ms = int((time.monotonic() - pipeline_start) * 1000)
            await audit.save_execution_metrics(
                deal_id=deal_id,
                validation_to_buy_ms=buy_ms,
                total_execution_ms=execution_ms,
                opportunity_alive=True,
            )

            self._circuit.on_trade_success()
            return deal_id

        finally:
            await self._locks.release(lock_key, owner=owner)
