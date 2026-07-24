"""Post-trade analytics — expected vs realized comparison.

After each completed deal, measures prediction accuracy:
  - Pricing error: expected vs actual sell price
  - Time error: expected vs actual hold time
  - PnL divergence: expected vs realized profit (KEY metric)

Auto-adjusts model parameters based on systematic errors.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from bot.models.database import Deal, async_session
from bot.models.types import DealState, PostTradeReport

logger = logging.getLogger(__name__)


class PostTradeAnalytics:
    async def analyze(self, deal: Deal) -> PostTradeReport | None:
        """Compute prediction errors for a completed deal."""
        if deal.state != DealState.SOLD.value:
            return None

        if not deal.expected_sell_price or not deal.actual_sell_price:
            return None

        expected_price = deal.expected_sell_price
        actual_price = deal.actual_sell_price

        pricing_error = abs(actual_price - expected_price) / max(expected_price, 1)

        expected_hours = deal.expected_sell_hours or 24.0
        actual_hours = deal.actual_sell_hours or 0.0
        time_error = abs(actual_hours - expected_hours) / max(expected_hours, 1)

        expected_roi = deal.expected_roi or 0.0
        actual_roi = deal.actual_roi or 0.0
        pnl_divergence = actual_roi - expected_roi

        report = PostTradeReport(
            deal_id=deal.id,
            pricing_error_pct=pricing_error * 100,
            time_prediction_error_pct=time_error * 100,
            realized_roi=actual_roi,
            predicted_roi=expected_roi,
            pnl_divergence=pnl_divergence,
        )

        logger.info(
            "PostTrade deal=%d: pricing_err=%.1f%% time_err=%.1f%% pnl_div=%.1f%%",
            deal.id,
            report.pricing_error_pct,
            report.time_prediction_error_pct,
            report.pnl_divergence,
        )

        return report

    async def get_recent_reports(self, limit: int = 50) -> list[PostTradeReport]:
        """Analyze recent completed deals."""
        async with async_session() as session:
            result = await session.execute(
                select(Deal)
                .where(
                    Deal.state == DealState.SOLD.value,
                    Deal.is_shadow.is_(False),
                    Deal.expected_sell_price.isnot(None),
                    Deal.actual_sell_price.isnot(None),
                )
                .order_by(Deal.sold_at.desc())
                .limit(limit)
            )
            deals = result.scalars().all()

        reports: list[PostTradeReport] = []
        for d in deals:
            r = await self.analyze(d)
            if r:
                reports.append(r)
        return reports

    async def get_model_adjustments(self) -> dict[str, float]:
        """Compute parameter adjustments based on systematic prediction errors.

        Returns recommended multipliers for model parameters.
        """
        reports = await self.get_recent_reports(limit=30)
        if len(reports) < 5:
            return {}

        # Average pricing error direction
        sum(r.pricing_error_pct for r in reports) / len(reports)
        avg_time_err = sum(r.time_prediction_error_pct for r in reports) / len(reports)
        avg_divergence = sum(r.pnl_divergence for r in reports) / len(reports)

        adjustments: dict[str, float] = {}

        # If consistently overestimating sell price → increase slippage buffer
        if avg_divergence < -5:  # realized < expected
            adjustments["slippage_buffer_mult"] = 1.2
            logger.info(
                "Recommend increasing slippage buffer: avg_divergence=%.1f%%",
                avg_divergence,
            )

        # If consistently underestimating hold time → increase hold estimate
        if avg_time_err > 50:
            adjustments["hold_time_mult"] = 1.3
            logger.info(
                "Recommend increasing hold time estimate: avg_time_err=%.1f%%",
                avg_time_err,
            )

        return adjustments

    async def get_summary_stats(self) -> dict[str, float]:
        """Summary statistics for observability dashboard."""
        reports = await self.get_recent_reports(limit=100)
        if not reports:
            return {}

        return {
            "total_completed": len(reports),
            "avg_pricing_error_pct": sum(r.pricing_error_pct for r in reports) / len(reports),
            "avg_time_error_pct": sum(r.time_prediction_error_pct for r in reports) / len(reports),
            "avg_pnl_divergence": sum(r.pnl_divergence for r in reports) / len(reports),
            "avg_realized_roi": sum(r.realized_roi for r in reports) / len(reports),
            "profitable_pct": sum(1 for r in reports if r.realized_roi > 0) / len(reports) * 100,
        }
