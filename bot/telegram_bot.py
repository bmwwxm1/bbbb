"""Telegram bot interface — commands, alerts, and transfer confirmation."""

import asyncio
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from bot.analyzer import Analyzer, DealSignal
from bot.config import settings
from bot.getgems_client import GetgemsClient
from bot.mrkt_client import MRKTClient, nanoton_to_ton
from bot.trader import Trader

logger = logging.getLogger(__name__)

router = Router()

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from typing import Callable, Awaitable

class AdminMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user:
            if user.id != settings.admin_chat_id:
                if isinstance(event, CallbackQuery):
                    await event.answer("Нет доступа.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("Нет доступа.")
                return None
        return await handler(event, data)

router.message.middleware(AdminMiddleware())
router.callback_query.middleware(AdminMiddleware())

_bot: Bot | None = None
_client: MRKTClient | None = None
_analyzer: Analyzer | None = None
_trader: Trader | None = None
_getgems: GetgemsClient | None = None
_scanner_running: bool = False
_waiting_for_setting: str | None = None

# Pending transfers: deal_id -> deal info (awaiting user confirmation)
_pending_transfers: dict[int, dict[str, Any]] = {}


def setup_components(
    bot: Bot,
    client: MRKTClient,
    analyzer: Analyzer,
    trader: Trader,
    getgems: GetgemsClient | None = None,
) -> None:
    global _bot, _client, _analyzer, _trader, _getgems
    _bot = bot
    _client = client
    _analyzer = analyzer
    _trader = trader
    _getgems = getgems


def _is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == settings.admin_chat_id


# ── Keyboard ────────────────────────────────────────────────────────────

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="📈 Рынок"), KeyboardButton(text="📋 Сделки")],
        [KeyboardButton(text="📂 Портфель"), KeyboardButton(text="⚙️ Настройки")],
        [
            KeyboardButton(text="▶️ Сканер ВКЛ"),
            KeyboardButton(text="⏸ Сканер ВЫКЛ"),
        ],
        [KeyboardButton(text="🔑 Обновить токен")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="status", description="Статус бота"),
        BotCommand(command="balance", description="Баланс MRKT"),
        BotCommand(command="market", description="Обзор рынка (MRKT vs Getgems)"),
        BotCommand(command="deals", description="Последние сделки"),
        BotCommand(command="portfolio", description="Портфель и статистика"),
        BotCommand(command="settings", description="Текущие настройки"),
        BotCommand(command="scanner_on", description="Включить сканер"),
        BotCommand(command="scanner_off", description="Выключить сканер"),
        BotCommand(command="token", description="Обновить MRKT токен"),
        BotCommand(command="help", description="Справка"),
    ]
    await bot.set_my_commands(commands)


# ── Commands ────────────────────────────────────────────────────────────


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_admin(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🤖 <b>MRKT ↔ Getgems Арбитраж Бот</b>\n\n"
        "Кросс-маркет арбитраж NFT-подарков.\n"
        "Бот сканирует цены на MRKT и Getgems, покупает дёшево — продаёт дорого.\n\n"
        "Ты делаешь одно: переводишь подарок @gemsrelayer когда бот просит.\n"
        "Всё остальное автоматически.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_admin(message):
        return
    await message.answer(
        "📖 <b>Как работает бот:</b>\n\n"
        "1. Сканирует цены на MRKT и Getgems каждые "
        f"{settings.scan_interval_seconds} сек\n"
        "2. Ищет разницу цен между маркетами\n"
        "3. Покупает на дешёвом маркете автоматически\n"
        "4. Выводит из MRKT → шлёт тебе уведомление\n"
        "5. Ты переводишь подарок @gemsrelayer и нажимаешь кнопку\n"
        "6. Бот автоматически выставляет на продажу на Getgems\n\n"
        "<b>Стратегии:</b>\n"
        "🌐 Кросс-маркет: MRKT → Getgems (или обратно)\n"
        "⚡ Арбитраж MRKT: листинг → ордер (мгновенно)\n"
        "💎 Глубокая скидка: цена ниже медианы на 40%+\n\n"
        f"<b>Мин. ROI:</b> {settings.min_roi_percent}%\n"
        f"<b>Комиссия Getgems:</b> {settings.getgems_sell_fee_pct}%\n"
        f"<b>Комиссия MRKT (продажа):</b> {settings.mrkt_sell_fee_pct}%",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not _is_admin(message) or not _analyzer:
        return

    collections_count = len(_analyzer.collections)
    matrix_combos = len(_analyzer.price_matrix)
    orders_combos = len(_analyzer.orders_by_combo)
    gg_floors = len(_analyzer.getgems_floors)
    pending = len(_pending_transfers)

    await message.answer(
        "📊 <b>Статус бота</b>\n\n"
        f"🔄 Сканер: {'✅ Работает' if _scanner_running else '⏸ Остановлен'}\n"
        f"📦 Коллекций: {collections_count}\n"
        f"🧮 Комбинаций: {matrix_combos}\n"
        f"📋 Ордеров: {orders_combos}\n"
        f"🌐 Getgems цен: {gg_floors}\n"
        f"⏳ Ожидают перевод: {pending}\n"
        f"⏱ Интервал: {settings.scan_interval_seconds} сек",
        parse_mode="HTML",
    )


@router.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    if not _is_admin(message) or not _client:
        return

    balance_data = await _client.get_balance()
    if not balance_data:
        await message.answer("❌ Не удалось получить баланс")
        return

    available = nanoton_to_ton(balance_data.get("hard", 0))
    total = nanoton_to_ton(balance_data.get("totalHard", 0))
    locked = nanoton_to_ton(balance_data.get("hardLocked", 0))
    stars = balance_data.get("stars", 0)
    await message.answer(
        "💰 <b>Баланс MRKT</b>\n\n"
        f"Доступно: <b>{available:.2f} TON</b>\n"
        f"Всего: {total:.2f} TON\n"
        f"Заблокировано: {locked:.2f} TON\n"
        f"⭐ Stars: {stars}",
        parse_mode="HTML",
    )


@router.message(Command("market"))
async def cmd_market(message: Message) -> None:
    if not _is_admin(message) or not _analyzer:
        return

    if not _analyzer.collections:
        await message.answer("⏳ Коллекции ещё не загружены...")
        return

    sorted_colls = sorted(_analyzer.collections, key=lambda c: c.get("volume", 0), reverse=True)[
        :15
    ]

    lines = ["📈 <b>Топ-15 коллекций (MRKT vs Getgems):</b>\n"]
    for i, c in enumerate(sorted_colls, 1):
        title = c.get("title", c.get("name", "?"))
        name = c.get("name", "")
        mrkt_floor = c.get("floorPriceNanoTons", 0)
        gg_floor = _analyzer.getgems_floors.get(name, 0)

        mrkt_str = f"{nanoton_to_ton(mrkt_floor):.2f}" if mrkt_floor else "—"
        gg_str = f"{nanoton_to_ton(gg_floor):.2f}" if gg_floor else "—"

        diff_str = ""
        if mrkt_floor > 0 and gg_floor > 0:
            diff_pct = ((gg_floor - mrkt_floor) / mrkt_floor) * 100
            if diff_pct > 5:
                diff_str = f" 🟢+{diff_pct:.0f}%"
            elif diff_pct < -5:
                diff_str = f" 🔴{diff_pct:.0f}%"

        lines.append(f"{i}. <b>{title}</b>\n   MRKT: {mrkt_str} | GG: {gg_str}{diff_str}")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("deals"))
async def cmd_deals(message: Message) -> None:
    if not _is_admin(message) or not _trader:
        return

    stats = await _trader.get_portfolio_stats()
    recent = stats.get("recent_deals", [])

    if not recent:
        await message.answer("📭 Сделок пока нет.")
        return

    lines = ["🔄 <b>Последние сделки:</b>\n"]
    for deal in recent[:10]:
        status_emoji = {
            "bought": "🛒",
            "listed": "📤",
            "sold": "✅",
            "withdrawn": "📥",
            "awaiting_transfer": "⏳",
            "listed_getgems": "🌐",
        }.get(deal.status, "❓")
        buy_ton = nanoton_to_ton(deal.buy_price)
        profit_str = ""
        if deal.profit:
            profit_str = f" → прибыль: {nanoton_to_ton(deal.profit):.2f} TON"

        signal = ""
        if deal.signal_type:
            signal = f" [{deal.signal_type}]"

        lines.append(
            f"{status_emoji} <b>{deal.collection_name}</b>"
            f"{' / ' + deal.model_name if deal.model_name else ''}"
            f" — {buy_ton:.2f} TON{profit_str}{signal}"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message) -> None:
    if not _is_admin(message) or not _trader:
        return

    stats = await _trader.get_portfolio_stats()

    await message.answer(
        "💼 <b>Портфель</b>\n\n"
        f"📊 Всего сделок: {stats['total_deals']}\n"
        f"✅ Продано: {stats['sold_deals']}\n"
        f"📤 На продаже: {stats['listed_deals']}\n"
        f"⏳ Ожидают перевод: {stats.get('pending_deals', 0)}\n\n"
        f"💰 Общая прибыль: {stats['total_profit_ton']:.2f} TON\n"
        f"💸 Всего потрачено: {stats['total_spent_ton']:.2f} TON\n"
        f"📈 Средний ROI: {stats['avg_roi']:.1f}%",
        parse_mode="HTML",
    )


# ── Settings ────────────────────────────────────────────────────────────


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"📈 Мин. ROI: {settings.min_roi_percent}%",
                    callback_data="set_roi",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅' if settings.auto_order_enabled else '❌'} Авто-ордера",
                    callback_data="toggle_orders",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅' if settings.cross_market_enabled else '❌'} Кросс-маркет",
                    callback_data="toggle_cross",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"💰 Мин. цена: {settings.auto_order_min_price_ton} TON",
                    callback_data="set_min_price",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"⏱ Интервал: {settings.scan_interval_seconds} сек",
                    callback_data="set_interval",
                ),
            ],
        ]
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    if not _is_admin(message):
        return

    max_trade = (
        "без лимита"
        if settings.max_trade_amount_ton <= 0
        else f"{settings.max_trade_amount_ton} TON"
    )
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"📈 Мин. ROI: <b>{settings.min_roi_percent}%</b>\n"
        f"🌐 Кросс-маркет: {'✅' if settings.cross_market_enabled else '❌'}\n"
        f"📝 Авто-ордера: {'✅' if settings.auto_order_enabled else '❌'}\n"
        f"💰 Мин. цена: {settings.auto_order_min_price_ton} TON\n"
        f"⏱ Интервал: {settings.scan_interval_seconds} сек\n"
        f"💰 Макс. сделка: {max_trade}\n"
        f"💱 Комиссия Getgems: {settings.getgems_sell_fee_pct}%\n"
        f"💱 Комиссия MRKT: {settings.mrkt_sell_fee_pct}%\n\n"
        "<i>Нажми кнопку чтобы изменить:</i>",
        parse_mode="HTML",
        reply_markup=_settings_keyboard(),
    )


@router.callback_query(F.data == "toggle_orders")
async def cb_toggle_orders(cb: CallbackQuery) -> None:
    settings.auto_order_enabled = not settings.auto_order_enabled
    state = "✅ Включены" if settings.auto_order_enabled else "❌ Выключены"
    await cb.answer(f"Авто-ордера: {state}")
    if cb.message:
        await cb.message.edit_reply_markup(reply_markup=_settings_keyboard())


@router.callback_query(F.data == "toggle_cross")
async def cb_toggle_cross(cb: CallbackQuery) -> None:
    settings.cross_market_enabled = not settings.cross_market_enabled
    state = "✅" if settings.cross_market_enabled else "❌"
    await cb.answer(f"Кросс-маркет: {state}")
    if cb.message:
        await cb.message.edit_reply_markup(reply_markup=_settings_keyboard())


@router.callback_query(F.data == "set_roi")
async def cb_set_roi(cb: CallbackQuery) -> None:
    global _waiting_for_setting
    _waiting_for_setting = "roi"
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"📈 Мин. ROI: <b>{settings.min_roi_percent}%</b>\n\n"
            "Отправь новое значение (например <code>10</code> или <code>15</code>):",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "set_min_price")
async def cb_set_min_price(cb: CallbackQuery) -> None:
    global _waiting_for_setting
    _waiting_for_setting = "min_price"
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"💰 Мин. цена: <b>{settings.auto_order_min_price_ton} TON</b>\n\n"
            "Отправь новое значение в TON:",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "set_interval")
async def cb_set_interval(cb: CallbackQuery) -> None:
    global _waiting_for_setting
    _waiting_for_setting = "interval"
    await cb.answer()
    if cb.message:
        await cb.message.answer(
            f"⏱ Интервал: <b>{settings.scan_interval_seconds} сек</b>\n\n"
            "Отправь новое значение в секундах:",
            parse_mode="HTML",
        )


@router.message(Command("scanner_on"))
async def cmd_scanner_on(message: Message) -> None:
    if not _is_admin(message):
        return
    global _scanner_running
    _scanner_running = True
    await message.answer("✅ Сканер запущен!")


@router.message(Command("scanner_off"))
async def cmd_scanner_off(message: Message) -> None:
    if not _is_admin(message):
        return
    global _scanner_running
    _scanner_running = False
    await message.answer("⏸ Сканер остановлен.")


@router.message(Command("token"))
async def cmd_token(message: Message) -> None:
    if not _is_admin(message):
        return
    if not _client or not message.text:
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "🔑 <b>Обновление токена MRKT</b>\n\n"
            "Использование: <code>/token ваш_новый_токен</code>",
            parse_mode="HTML",
        )
        return

    new_token = parts[1].strip()
    _client.update_token(new_token)

    try:
        await message.delete()
    except Exception:
        pass

    await message.answer("🔑 Токен обновлён! Проверяю...", parse_mode="HTML")

    async def _verify_token() -> None:
        if not _client or not _bot:
            return
        try:
            me = await asyncio.wait_for(_client.get_me(), timeout=15)
            if me:
                name = me.get("fullName", "Unknown")
                await _bot.send_message(
                    settings.admin_chat_id,
                    f"✅ Токен подтверждён!\nАккаунт: <b>{name}</b>",
                    parse_mode="HTML",
                )
            else:
                await _bot.send_message(
                    settings.admin_chat_id,
                    "⚠️ Токен установлен, но проверка не удалась.",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning("Token verify failed: %s", e)

    asyncio.create_task(_verify_token())


# ── Transfer Confirmation (cross-market flow) ───────────────────────────


async def send_transfer_request(
    deal_id: int,
    gift_id: str,
    collection_name: str,
    model_name: str,
    buy_price: int,
    target_sell_price: int,
) -> None:
    """Send a notification asking user to transfer gift to @gemsrelayer."""
    if not _bot:
        return

    buy_ton = nanoton_to_ton(buy_price)
    sell_ton = nanoton_to_ton(target_sell_price)
    profit = target_sell_price * (1 - settings.getgems_sell_fee_pct / 100) - buy_price
    profit_ton = nanoton_to_ton(int(profit))
    roi = (profit / buy_price) * 100 if buy_price > 0 else 0

    _pending_transfers[deal_id] = {
        "gift_id": gift_id,
        "collection_name": collection_name,
        "target_sell_price": target_sell_price,
    }

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить перевод",
                    callback_data=f"confirm_transfer:{deal_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"cancel_transfer:{deal_id}",
                )
            ],
        ]
    )

    display = collection_name
    if model_name:
        display += f" / {model_name}"

    await _bot.send_message(
        settings.admin_chat_id,
        f"🎯 <b>Переведи подарок!</b>\n\n"
        f"📦 {display}\n"
        f"💰 Купил: <b>{buy_ton:.2f} TON</b>\n"
        f"🎯 Продам на Getgems: <b>{sell_ton:.2f} TON</b>\n"
        f"💵 Прибыль: ~{profit_ton:.2f} TON (ROI {roi:.0f}%)\n\n"
        f"👉 Переведи подарок боту <b>@gemsrelayer</b>\n"
        f"и нажми кнопку ниже:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("confirm_transfer:"))
async def cb_confirm_transfer(cb: CallbackQuery) -> None:
    deal_id_str = cb.data.split(":")[1] if cb.data else ""  # type: ignore[union-attr]
    try:
        deal_id = int(deal_id_str)
    except ValueError:
        await cb.answer("Ошибка: неверный ID")
        return

    transfer = _pending_transfers.pop(deal_id, None)
    if not transfer:
        await cb.answer("Этот перевод уже обработан")
        return

    await cb.answer("Принято! Ищу подарок на Getgems...")

    if cb.message:
        await cb.message.edit_text(
            cb.message.text + "\n\n⏳ <b>Ищу подарок на Getgems...</b>",  # type: ignore[operator]
            parse_mode="HTML",
        )

    # Update deal status
    from bot.database import Deal, async_session

    async with async_session() as session:
        db_deal = await session.get(Deal, deal_id)
        if db_deal:
            db_deal.status = "awaiting_transfer"
            await session.commit()

    # Start monitoring for the gift to appear on Getgems
    asyncio.create_task(
        _monitor_and_list(
            deal_id=deal_id,
            collection_name=transfer["collection_name"],
            target_sell_price=transfer["target_sell_price"],
        )
    )


@router.callback_query(F.data.startswith("cancel_transfer:"))
async def cb_cancel_transfer(cb: CallbackQuery) -> None:
    deal_id_str = cb.data.split(":")[1] if cb.data else ""  # type: ignore[union-attr]
    try:
        deal_id = int(deal_id_str)
    except ValueError:
        await cb.answer("Ошибка")
        return

    _pending_transfers.pop(deal_id, None)
    await cb.answer("Отменено")

    if cb.message:
        await cb.message.edit_text(
            cb.message.text + "\n\n❌ <b>Отменено</b>",  # type: ignore[operator]
            parse_mode="HTML",
        )


async def _monitor_and_list(
    deal_id: int,
    collection_name: str,
    target_sell_price: int,
    max_attempts: int = 60,
    interval: int = 30,
) -> None:
    """Monitor Getgems for the gift to appear, then auto-list it."""
    if not _getgems or not _bot:
        return

    for attempt in range(max_attempts):
        await asyncio.sleep(interval)

        try:
            listings = await _getgems.get_gift_listings(
                collection_name=collection_name,
                count=20,
                sort_asc=True,
            )

            # Look for our gift (recently appeared, owned by our wallet)
            # For now, we list at target price once we detect
            # the gift on Getgems
            # TODO: match by specific NFT address from withdrawal

            if listings:
                # Try to find an unlisted gift owned by us
                # For now, proceed with auto-listing
                nft_address = None
                for item in listings:
                    # We need the NFT address to list
                    if item.get("nft_address"):
                        nft_address = item["nft_address"]
                        break

                if nft_address:
                    success = await _getgems.list_gift_for_sale(
                        nft_address,
                        target_sell_price,
                    )
                    if success:
                        sell_ton = nanoton_to_ton(target_sell_price)
                        await _bot.send_message(
                            settings.admin_chat_id,
                            f"🌐 <b>Выставлено на Getgems!</b>\n\n"
                            f"📦 {collection_name}\n"
                            f"🏷 Цена: <b>{sell_ton:.2f} TON</b>\n"
                            f"✅ Ожидаем продажу",
                            parse_mode="HTML",
                        )

                        from bot.database import Deal, async_session

                        async with async_session() as session:
                            db_deal = await session.get(Deal, deal_id)
                            if db_deal:
                                db_deal.status = "listed_getgems"
                                db_deal.sell_price = target_sell_price
                                db_deal.sell_type = "getgems"
                                import datetime

                                db_deal.listed_at = datetime.datetime.now(datetime.UTC)
                                await session.commit()
                        return

        except Exception as e:
            logger.warning("Monitor attempt %d failed: %s", attempt, e)

    # Timed out
    await _bot.send_message(
        settings.admin_chat_id,
        f"⚠️ Не нашёл подарок <b>{collection_name}</b> на Getgems "
        f"после {max_attempts * interval // 60} мин.\n"
        "Проверь, что перевод @gemsrelayer прошёл.",
        parse_mode="HTML",
    )


# ── Button handlers (ReplyKeyboard) ─────────────────────────────────────

_BUTTON_MAP = {
    "📊 Статус": "status",
    "💰 Баланс": "balance",
    "📈 Рынок": "market",
    "📋 Сделки": "deals",
    "📂 Портфель": "portfolio",
    "⚙️ Настройки": "settings",
    "▶️ Сканер ВКЛ": "scanner_on",
    "⏸ Сканер ВЫКЛ": "scanner_off",
    "🔑 Обновить токен": "token_help",
}

_BUTTON_HANDLERS = {
    "status": lambda m: cmd_status(m),
    "balance": lambda m: cmd_balance(m),
    "market": lambda m: cmd_market(m),
    "deals": lambda m: cmd_deals(m),
    "portfolio": lambda m: cmd_portfolio(m),
    "settings": lambda m: cmd_settings(m),
    "scanner_on": lambda m: cmd_scanner_on(m),
    "scanner_off": lambda m: cmd_scanner_off(m),
    "token_help": lambda m: cmd_token(m),
}


@router.message(F.text.in_(_BUTTON_MAP))
async def handle_button(message: Message) -> None:
    if not _is_admin(message) or not message.text:
        return
    cmd = _BUTTON_MAP.get(message.text)
    if not cmd:
        stripped = message.text.replace("\ufe0f", "")
        cmd = _BUTTON_MAP.get(stripped)
    if not cmd:
        for key, val in _BUTTON_MAP.items():
            if key.replace("\ufe0f", "") == message.text.replace("\ufe0f", ""):
                cmd = val
                break
    handler = _BUTTON_HANDLERS.get(cmd) if cmd else None
    if handler:
        await handler(message)


@router.message(F.text)
async def handle_setting_input(message: Message) -> None:
    global _waiting_for_setting
    if not _is_admin(message) or not _waiting_for_setting or not message.text:
        return

    text = message.text.strip().replace(",", ".")
    setting_name = _waiting_for_setting
    _waiting_for_setting = None

    try:
        if setting_name == "roi":
            val = float(text)
            if val < 1 or val > 100:
                await message.answer("❌ Значение от 1 до 100%")
                return
            settings.min_roi_percent = val
            await message.answer(
                f"✅ Мин. ROI: <b>{val}%</b>",
                parse_mode="HTML",
            )
        elif setting_name == "min_price":
            val = float(text)
            if val < 0.1 or val > 1000:
                await message.answer("❌ Значение от 0.1 до 1000 TON")
                return
            settings.auto_order_min_price_ton = val
            await message.answer(
                f"✅ Мин. цена: <b>{val} TON</b>",
                parse_mode="HTML",
            )
        elif setting_name == "interval":
            val_int = int(float(text))
            if val_int < 5 or val_int > 600:
                await message.answer("❌ Значение от 5 до 600 сек")
                return
            settings.scan_interval_seconds = val_int
            await message.answer(
                f"✅ Интервал: <b>{val_int} сек</b>",
                parse_mode="HTML",
            )
        else:
            await message.answer("❌ Неизвестная настройка")
    except (ValueError, TypeError):
        await message.answer("❌ Введи число", parse_mode="HTML")


# ── Core functions ──────────────────────────────────────────────────────


def is_scanner_running() -> bool:
    return _scanner_running


def set_scanner_running(value: bool) -> None:
    global _scanner_running
    _scanner_running = value


# ── Alert Formatting ───────────────────────────────────────────────────


def format_deal_alert(deal: DealSignal) -> str:
    signal_map = {
        "cross_market": ("🌐", "Кросс-маркет (MRKT → Getgems)"),
        "mrkt_arbitrage": ("⚡", "Арбитраж MRKT (листинг → ордер)"),
        "deep_discount": ("💎", "Глубокая скидка"),
    }
    emoji, text = signal_map.get(deal.signal_type, ("❓", deal.signal_type))

    lines = [
        f"{emoji} <b>СДЕЛКА: {text}</b>\n",
        f"📦 <b>{deal.combo.collection}</b>",
    ]
    if deal.combo.model:
        lines.append(f"🎨 {deal.combo.model}")

    lines.append(f"\n💰 Покупка: <b>{deal.listing_price_ton:.2f} TON</b>")

    if deal.signal_type == "cross_market" and deal.getgems_sell_price > 0:
        gg_ton = nanoton_to_ton(deal.getgems_sell_price)
        lines.append(f"🎯 Продажа (Getgems): <b>{gg_ton:.2f} TON</b>")
    elif deal.order_price > 0:
        lines.append(f"📋 Ордер MRKT: {deal.order_price_ton:.2f} TON")

    lines.append(f"\n💵 Прибыль: <b>{deal.profit_ton:.2f} TON</b>")
    lines.append(f"📈 ROI: <b>{deal.roi_percent:.1f}%</b>")

    return "\n".join(lines)


def format_trade_result(deal: DealSignal, result: dict[str, Any]) -> str:
    action = result.get("action", "unknown")

    if action == "withdrawn":
        buy_ton = nanoton_to_ton(result.get("buy_price", 0))
        sell_ton = nanoton_to_ton(result.get("target_sell_price", 0))
        return (
            f"📥 <b>Выведено из MRKT</b>\n\n"
            f"📦 {result.get('collection', deal.combo.display)}\n"
            f"💰 Купил: {buy_ton:.2f} TON\n"
            f"🎯 Цель (Getgems): {sell_ton:.2f} TON\n\n"
            f"⏳ Жду уведомление о переводе..."
        )
    elif action == "sold_to_order":
        profit = nanoton_to_ton(result.get("profit", 0))
        return (
            f"✅ <b>ПРОДАНО В ОРДЕР (MRKT)</b>\n\n"
            f"📦 {deal.combo.display}\n"
            f"💰 Купил: {deal.listing_price_ton:.2f} TON\n"
            f"💵 Продал: {deal.order_price_ton:.2f} TON\n"
            f"🎉 Прибыль: <b>{profit:.2f} TON</b>"
        )
    elif action == "listed":
        sell_price = nanoton_to_ton(result.get("sell_price", 0))
        return (
            f"📤 <b>ВЫСТАВЛЕНО НА MRKT</b>\n\n"
            f"📦 {deal.combo.display}\n"
            f"💰 Купил: {deal.listing_price_ton:.2f} TON\n"
            f"🏷 Цена: {sell_price:.2f} TON"
        )
    return f"❓ {action}"


async def send_alert(text: str) -> None:
    if _bot:
        try:
            await _bot.send_message(settings.admin_chat_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to send alert: %s", e)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
