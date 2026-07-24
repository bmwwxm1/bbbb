"""Notification service — structured alerts to Telegram.

Notification types:
  - Trade executed (buy confirmation)
  - Transfer request (user action required)
  - Listing confirmed
  - Sale confirmed
  - Risk alert (circuit breaker, stuck inventory)
  - System alert (errors, restarts)
"""

from __future__ import annotations

import logging
from typing import Any

from bot.models.types import nanoton_to_ton

logger = logging.getLogger(__name__)


class NotificationService:
    """Formats and sends notifications. Bot instance injected at runtime."""

    def __init__(self) -> None:
        self._send_fn: Any = None  # set to bot.send_message at startup
        self._chat_id: int = 0

    def configure(self, send_fn: Any, chat_id: int) -> None:
        self._send_fn = send_fn
        self._chat_id = chat_id

    async def notify_transfer_needed(
        self,
        deal_id: int,
        collection: str,
        gift_id: str,
        buy_price: int,
        target_sell_price: int,
        expected_roi: float,
    ) -> None:
        """Ask user to transfer gift to @gemsrelayer."""
        profit_ton = nanoton_to_ton(target_sell_price - buy_price)
        text = (
            f"🎯 Переведи подарок\n\n"
            f"📦 {collection}\n"
            f"💰 Купил: {nanoton_to_ton(buy_price):.2f} TON\n"
            f"🎯 Продам на Getgems: {nanoton_to_ton(target_sell_price):.2f} TON\n"
            f"📈 Прибыль: ~{profit_ton:.2f} TON (ROI {expected_roi:.0f}%)\n\n"
            f"Переведи подарок боту @gemsrelayer и нажми кнопку ниже"
        )
        await self._send(text, reply_markup=self._transfer_keyboard(deal_id))

    async def notify_buy_executed(
        self,
        deal_id: int,
        collection: str,
        price: int,
        strategy: str,
        is_shadow: bool,
    ) -> None:
        prefix = "👻 SHADOW" if is_shadow else "✅"
        text = (
            f"{prefix} Покупка\n\n"
            f"📦 {collection}\n"
            f"💰 {nanoton_to_ton(price):.2f} TON\n"
            f"📋 Стратегия: {strategy}"
        )
        await self._send(text)

    async def notify_listing_confirmed(
        self,
        deal_id: int,
        collection: str,
        price: int,
        market: str,
    ) -> None:
        text = (
            f"📤 Выставлен на продажу\n\n"
            f"📦 {collection}\n"
            f"💰 {nanoton_to_ton(price):.2f} TON\n"
            f"🏪 {market}"
        )
        await self._send(text)

    async def notify_sold(
        self,
        deal_id: int,
        collection: str,
        sell_price: int,
        profit: int,
        roi: float,
    ) -> None:
        text = (
            f"💰 Продано!\n\n"
            f"📦 {collection}\n"
            f"💵 Цена: {nanoton_to_ton(sell_price):.2f} TON\n"
            f"📈 Прибыль: {nanoton_to_ton(profit):.2f} TON ({roi:.0f}%)"
        )
        await self._send(text)

    async def notify_risk_alert(self, message: str) -> None:
        await self._send(f"⚠️ Risk Alert\n{message}")

    async def notify_system(self, message: str) -> None:
        await self._send(f"🔧 System\n{message}")

    async def _send(self, text: str, reply_markup: Any = None) -> None:
        if not self._send_fn or not self._chat_id:
            logger.warning("Notification not configured: %s", text[:80])
            return
        try:
            await self._send_fn(
                self._chat_id,
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception("Failed to send notification")

    @staticmethod
    def _transfer_keyboard(deal_id: int) -> Any:
        """Build inline keyboard with transfer confirmation button.

        Returns dict structure compatible with aiogram InlineKeyboardMarkup.
        Actual keyboard construction happens in telegram_bot.py.
        """
        return {"deal_id": deal_id, "type": "transfer_confirm"}
