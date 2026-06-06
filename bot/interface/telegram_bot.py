"""Telegram bot interface — commands, inline navigation, inventory management.

Features:
  - Persistent keyboard with main actions
  - Inventory browser with inline pagination
  - Price comparison MRKT vs Getgems per gift
  - Sell/cancel/reprice from the bot
  - System status, portfolio, market overview
"""

from __future__ import annotations

import logging
import time
from typing import Any

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.core import state_machine
from bot.execution.portfolio_manager import PortfolioManager
from bot.intelligence.opportunity_scorer import set_runtime_thresholds as _set_scorer
from bot.models.types import DealState

logger = logging.getLogger(__name__)

router = Router()

NANO = 1_000_000_000


def _ton(nano: int) -> str:
    """Format nanoTON to human-readable TON string."""
    return f"{nano / NANO:.2f}"


# ── State (set at startup) ─────────────────────────────────────────────

_state: dict[str, Any] = {
    "scanner": None,
    "circuit": None,
    "risk": None,
    "mds": None,
    "mrkt": None,
    "gg": None,
    "start_time": time.time(),
    "portfolio": PortfolioManager(),
    "admin_chat_id": 0,
}

# Per-user cache for inventory pagination
_user_inventory: dict[int, list[dict[str, Any]]] = {}

# Pending Getgems transfers: short_key -> {gift_id, name, price}
_gg_pending: dict[str, dict[str, Any]] = {}
_gg_counter = 0

# ── Runtime settings (editable via bot) ────────────────────────────────
_runtime: dict[str, float] = {
    "min_roi_pct": 25.0,  # min ROI for auto-buy on Getgems
    "cross_roi_pct": 25.0,  # min ROI for cross-market (scanner)
    "order_roi_pct": 25.0,  # min ROI for order arb
    "deep_roi_pct": 25.0,  # min ROI for deep discount
    "scan_interval": 60.0,  # seconds between scans
    "max_buy_ton": 60.0,  # max TON per single buy
    "max_exposure_ton": 600.0,  # max total exposure
    "autosell_interval": 120.0,  # auto-seller check interval
}

# ── Sell price mode: "floor" (listing floor) or "instant" (best buy order)
_sell_price_mode: dict[str, str] = {
    "mrkt": "instant",
    "portal": "instant",
}

# ── Market toggles (editable via bot) ─────────────────────────────────
_market_enabled: dict[str, dict[str, bool]] = {
    "mrkt": {"buy": True, "sell": True},
    "getgems": {"buy": True, "sell": True},
    "fragment": {"buy": True, "sell": True},
    "portal": {"buy": True, "sell": True},
}

# Ordered list for numbered menu
_SETTING_KEYS: list[str] = [
    "min_roi_pct",
    "cross_roi_pct",
    "order_roi_pct",
    "deep_roi_pct",
    "scan_interval",
    "max_buy_ton",
    "max_exposure_ton",
    "autosell_interval",
]

# State for conversational settings flow: user_id -> {"key": runtime_key}
_waiting_setting: dict[int, str] = {}

_set_scorer(min_roi_pct=_runtime["min_roi_pct"])


def get_runtime(key: str) -> float:
    return _runtime.get(key, 0.0)


def set_runtime(key: str, value: float) -> None:
    _runtime[key] = value


def is_market_enabled(market: str) -> bool:
    entry = _market_enabled.get(market)
    if entry is None:
        return True
    return entry.get("buy", True) or entry.get("sell", True)


def is_market_buy_enabled(market: str) -> bool:
    entry = _market_enabled.get(market)
    if entry is None:
        return True
    return entry.get("buy", True)


def is_market_sell_enabled(market: str) -> bool:
    entry = _market_enabled.get(market)
    if entry is None:
        return True
    return entry.get("sell", True)


def set_market_enabled(market: str, enabled: bool) -> None:
    if market not in _market_enabled:
        _market_enabled[market] = {"buy": True, "sell": True}
    _market_enabled[market]["buy"] = enabled
    _market_enabled[market]["sell"] = enabled


def set_market_buy_enabled(market: str, enabled: bool) -> None:
    if market not in _market_enabled:
        _market_enabled[market] = {"buy": True, "sell": True}
    _market_enabled[market]["buy"] = enabled


def set_market_sell_enabled(market: str, enabled: bool) -> None:
    if market not in _market_enabled:
        _market_enabled[market] = {"buy": True, "sell": True}
    _market_enabled[market]["sell"] = enabled


def get_sell_price_mode(market: str) -> str:
    return _sell_price_mode.get(market, "floor")


def set_sell_price_mode(market: str, mode: str) -> None:
    if mode in ("floor", "instant"):
        _sell_price_mode[market] = mode


def _is_admin(user_id: int) -> bool:
    """Check if user is admin (defensive — always required for state-changing commands)."""
    admin = _state.get("admin_chat_id", 0)
    return admin > 0 and user_id == admin


def configure(
    admin_chat_id: int,
    scanner: Any = None,
    circuit: Any = None,
    risk: Any = None,
    mds: Any = None,
    mrkt: Any = None,
    gg: Any = None,
) -> None:
    _state["admin_chat_id"] = admin_chat_id
    _state["scanner"] = scanner
    _state["circuit"] = circuit
    _state["risk"] = risk
    _state["mds"] = mds
    _state["mrkt"] = mrkt
    _state["gg"] = gg


# ── Keyboards ──────────────────────────────────────────────────────────

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎁 Инвентарь"), KeyboardButton(text="📊 Статус")],
        [KeyboardButton(text="📈 Рынок"), KeyboardButton(text="💰 Портфель")],
        [KeyboardButton(text="🛡 Риски"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="📦 Подарки"), KeyboardButton(text="📉 Статистика")],
    ],
    resize_keyboard=True,
)


def _is_on_sale(gift: dict) -> bool:
    """Check if a gift is currently listed for sale on MRKT."""
    # The correct field from MRKT API is `isOnSale`
    if gift.get("isOnSale") is True:
        return True
    if gift.get("isOnAuction") is True:
        return True
    return False


def _inv_page_kb(gifts: list[dict], page: int, per_page: int = 5) -> InlineKeyboardMarkup:
    """Build inline keyboard for inventory page."""
    start = page * per_page
    end = min(start + per_page, len(gifts))
    total_pages = max(1, (len(gifts) + per_page - 1) // per_page)

    buttons: list[list[InlineKeyboardButton]] = []

    source_icons = {"mrkt": "🟦", "getgems": "💎", "portal": "🟣"}
    for g in gifts[start:end]:
        gift_id = g.get("id", "")
        name = g.get("collectionName", "?")
        number = g.get("number", "")
        source = g.get("_source", "mrkt")
        on_sale = _is_on_sale(g) or g.get("sale") or g.get("status") == "listed"
        price = g.get("salePrice", 0) or 0

        icon = source_icons.get(source, "🎁")
        sale_icon = "🏷" if on_sale else icon
        label = f"{sale_icon} {name} #{number}"
        if on_sale and price > 0:
            label += f" — {_ton(price)} TON"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"gift:{gift_id}",
                )
            ]
        )

    # Pagination row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"inv_page:{page - 1}"))
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="noop",
        )
    )
    if end < len(gifts):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"inv_page:{page + 1}"))
    buttons.append(nav)

    # Refresh button
    buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="inv_refresh")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _gift_detail_kb(
    gift_id: str,
    on_sale: bool,
    best_market: str = "",
    best_price: int = 0,
) -> InlineKeyboardMarkup:
    """Inline keyboard for a single gift detail view."""
    buttons: list[list[InlineKeyboardButton]] = []

    if on_sale:
        buttons.append(
            [
                InlineKeyboardButton(text="💲 Изменить цену", callback_data=f"reprice:{gift_id}"),
                InlineKeyboardButton(text="❌ Снять с продажи", callback_data=f"unsell:{gift_id}"),
            ]
        )
    else:
        # Auto-recommendation button
        if best_market and best_price > 0:
            if best_market == "mrkt":
                rec_label = f"⚡ Продать на MRKT за {_ton(best_price)} TON"
            else:
                rec_label = f"⚡ Продать на Getgems за {_ton(best_price)} TON"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=rec_label,
                        callback_data=f"sell_best:{gift_id}:{best_market}:{best_price}",
                    ),
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(text="🏷 MRKT", callback_data=f"sell_mrkt:{gift_id}"),
                InlineKeyboardButton(text="💎 Getgems", callback_data=f"sell_gg:{gift_id}"),
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(text="📊 Сравнить цены", callback_data=f"compare:{gift_id}"),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="inv_page:0"),
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _price_buttons(gift_id: str, floor_price: int, market: str) -> InlineKeyboardMarkup:
    """Quick price selection buttons relative to floor."""
    prefix = "set_price_mrkt" if market == "mrkt" else "set_price_gg"
    buttons: list[list[InlineKeyboardButton]] = []

    # Price options: floor, floor-5%, floor-10%, floor+5%, custom
    offsets = [
        ("Пол", 1.0),
        ("-5%", 0.95),
        ("-10%", 0.90),
        ("+5%", 1.05),
        ("+10%", 1.10),
    ]
    row: list[InlineKeyboardButton] = []
    for label, mult in offsets:
        price = int(floor_price * mult)
        # Round to 0.1 TON
        price = (price // 100_000_000) * 100_000_000
        row.append(
            InlineKeyboardButton(
                text=f"{label} ({_ton(price)})",
                callback_data=f"{prefix}:{gift_id}:{price}",
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"gift:{gift_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def make_transfer_keyboard(deal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить перевод",
                    callback_data=f"transfer_confirm:{deal_id}",
                )
            ]
        ]
    )


# ── Command Handlers ───────────────────────────────────────────────────


@router.message(Command("start"))
async def cmd_start(message: types.Message) -> None:
    # Auto-update admin chat ID on first interaction
    if message.chat.id and _state.get("admin_chat_id") != message.chat.id:
        _state["admin_chat_id"] = message.chat.id
        notif = _state.get("notification")
        if notif:
            notif._chat_id = message.chat.id
        logger.info("Admin chat ID updated to %s", message.chat.id)
    await message.answer(
        "🤖 <b>MRKT Торговый Бот</b>\n\n"
        "Используй кнопки ниже для управления.\n\n"
        "🎁 <b>Инвентарь</b> — просмотр и продажа подарков\n"
        "📊 <b>Статус</b> — состояние системы\n"
        "📈 <b>Рынок</b> — цены на площадках\n"
        "💰 <b>Портфель</b> — подарки и unrealized P&L\n"
        "📉 <b>Статистика</b> — P&L, сделки, комиссии\n"
        "🛡 <b>Риски</b> — лимиты и экспозиция\n"
        "⚙️ <b>Настройки</b> — теневой режим, пауза\n"
        "📦 <b>Подарки</b> — подарки в Telegram\n\n"
        "<b>Команды:</b>\n"
        "/stats [day|week|30] — статистика за период\n"
        "/portfolio — текущий портфель\n"
        '/sold "Name" price [mrkt|getgems] — ручная продажа',
        reply_markup=MAIN_KB,
        parse_mode="HTML",
    )


# ── Inventory ──────────────────────────────────────────────────────────


async def _load_inventory(user_id: int) -> list[dict[str, Any]]:
    """Fetch inventory from all markets (MRKT, Getgems, Portal)."""
    all_gifts: list[dict[str, Any]] = []

    # MRKT
    mrkt = _state["mrkt"]
    if mrkt:
        try:
            mrkt_gifts = await mrkt.get_my_gifts(
                owner_tg_id=_state["admin_chat_id"], count=100,
            )
            for g in mrkt_gifts:
                g["_source"] = "mrkt"
            all_gifts.extend(mrkt_gifts)
        except Exception:
            pass

    mds = _state["mds"]

    # Getgems
    gg = mds._gg if mds and hasattr(mds, "_gg") else None
    if gg and gg._wallet_address:
        try:
            gg_gifts = await gg.get_user_offchain_gifts(gg._wallet_address)
            for g in gg_gifts:
                g["_source"] = "getgems"
                if not g.get("collectionName"):
                    g["collectionName"] = g.get("name", "?").split("#")[0].strip()
                if not g.get("number"):
                    raw = g.get("name", "")
                    if "#" in raw:
                        g["number"] = raw.split("#")[-1].strip()
                if not g.get("id"):
                    g["id"] = f"gg_{g.get('address', '')}"
            all_gifts.extend(gg_gifts)
        except Exception:
            pass

    # Portal
    portal_c = mds._portal if mds and hasattr(mds, "_portal") else None
    if portal_c and portal_c.authenticated:
        try:
            owned = await portal_c.get_owned_nfts(limit=100)
            portal_nfts = (owned or {}).get("nfts", [])
            for g in portal_nfts:
                g["_source"] = "portal"
                if not g.get("collectionName"):
                    g["collectionName"] = g.get("name", "?").split("#")[0].strip()
                if not g.get("number"):
                    raw = g.get("name", "")
                    if "#" in raw:
                        g["number"] = raw.split("#")[-1].strip()
                if not g.get("id"):
                    g["id"] = f"portal_{g.get('id', '')}"
            all_gifts.extend(portal_nfts)
        except Exception:
            pass

    _user_inventory[user_id] = all_gifts
    return all_gifts


async def _send_inventory(message: types.Message, page: int = 0) -> None:
    user_id = message.from_user.id if message.from_user else 0
    gifts = _user_inventory.get(user_id)

    if gifts is None:
        await message.answer("⏳ Загружаю инвентарь...")
        gifts = await _load_inventory(user_id)

    if not gifts:
        await message.answer(
            "🎁 <b>Инвентарь пуст</b>\n\nНет подарков ни на одном маркете.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    # Count per market
    mrkt_c = sum(1 for g in gifts if g.get("_source") == "mrkt")
    gg_c = sum(1 for g in gifts if g.get("_source") == "getgems")
    portal_c = sum(1 for g in gifts if g.get("_source") == "portal")
    on_sale_count = sum(1 for g in gifts if _is_on_sale(g))
    total_value = sum(g.get("salePrice", 0) or 0 for g in gifts if _is_on_sale(g))

    header = f"🎁 <b>Инвентарь</b> ({len(gifts)} шт.)\n"
    parts_line: list[str] = []
    if mrkt_c:
        parts_line.append(f"🟦 MRKT: {mrkt_c}")
    if gg_c:
        parts_line.append(f"💎 GG: {gg_c}")
    if portal_c:
        parts_line.append(f"🟣 Portal: {portal_c}")
    if parts_line:
        header += " | ".join(parts_line) + "\n"
    if on_sale_count:
        header += f"🏷 На продаже: {on_sale_count}\n"
    if total_value > 0:
        header += f"💰 Сумма листингов: {_ton(total_value)} TON\n"

    await message.answer(
        header,
        parse_mode="HTML",
        reply_markup=_inv_page_kb(gifts, page),
    )


@router.message(Command("inventory"))
async def cmd_inventory(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    _user_inventory.pop(user_id, None)  # force refresh
    await _send_inventory(message)


@router.message(F.text == "🎁 Инвентарь")
async def btn_inventory(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    _user_inventory.pop(user_id, None)
    await _send_inventory(message)


@router.callback_query(F.data.startswith("inv_page:"))
async def cb_inv_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])

    if not gifts:
        gifts = await _load_inventory(user_id)

    if callback.message:
        on_sale_count = sum(1 for g in gifts if _is_on_sale(g))
        not_listed = len(gifts) - on_sale_count
        header = (
            f"🎁 <b>Инвентарь</b> ({len(gifts)} шт.)\n"
            f"🏷 На продаже: {on_sale_count}\n"
            f"📦 Не выставлено: {not_listed}\n"
        )

        try:
            await callback.message.edit_text(
                header,
                parse_mode="HTML",
                reply_markup=_inv_page_kb(gifts, page),
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data == "inv_refresh")
async def cb_inv_refresh(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    _user_inventory.pop(user_id, None)
    gifts = await _load_inventory(user_id)

    if callback.message:
        on_sale_count = sum(1 for g in gifts if _is_on_sale(g))
        not_listed = len(gifts) - on_sale_count
        header = (
            f"🎁 <b>Инвентарь</b> ({len(gifts)} шт.)\n"
            f"🏷 На продаже: {on_sale_count}\n"
            f"📦 Не выставлено: {not_listed}\n"
        )
        try:
            await callback.message.edit_text(
                header,
                parse_mode="HTML",
                reply_markup=_inv_page_kb(gifts, 0),
            )
        except Exception:
            pass
    await callback.answer("🔄 Обновлено")


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ── Gift Detail ────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("gift:"))
async def cb_gift_detail(callback: CallbackQuery) -> None:
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]
    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])

    gift = next((g for g in gifts if g.get("id") == gift_id), None)

    if not gift:
        mrkt = _state["mrkt"]
        if mrkt:
            gift = await mrkt.get_gift_details(gift_id)

    if not gift:
        await callback.answer("Подарок не найден")
        return

    # Debug: log raw gift fields for audit
    logger.info(
        "Gift detail raw: id=%s isOnSale=%s isOnAuction=%s salePrice=%s floorByCollection=%s",
        gift.get("id"),
        gift.get("isOnSale"),
        gift.get("isOnAuction"),
        gift.get("salePrice"),
        gift.get("floorPriceNanoTONsByCollection"),
    )

    name = gift.get("collectionName", "?")
    number = gift.get("number", "?")
    model = gift.get("modelName", "")
    backdrop = gift.get("backdropName", "")
    on_sale = _is_on_sale(gift)

    # salePrice from API = listing price when on sale, OR collection floor when not
    sale_price = gift.get("salePrice", 0) or 0
    # floorPriceNanoTONsByCollection = MRKT floor from the gift object itself
    mrkt_floor_from_gift = gift.get("floorPriceNanoTONsByCollection", 0) or 0

    # Get floors for comparison
    mds = _state["mds"]
    mrkt_client = _state["mrkt"]
    gg_floor = 0
    mrkt_floor = mrkt_floor_from_gift

    if mds:
        gg_floor = mds.get_cached_gg_floor(name)
    frag_floor = mds.get_cached_fragment_floor(name) if mds else 0
    portal_floor = mds.get_cached_portal_floor(name) if mds else 0

    # If no floor from gift, try fetching from listings
    if mrkt_floor <= 0 and mrkt_client:
        try:
            listings = await mrkt_client.get_listings(
                collection_names=[name],
                count=1,
                ordering="Price",
                low_to_high=True,
            )
            first = (listings.get("gifts") or [None])[0]
            if first:
                mrkt_floor = first.get("salePrice", 0) or 0
        except Exception:
            pass

    # Determine best market (4-way: MRKT, Getgems, Fragment, Portal)
    best_market = ""
    best_price = 0
    ref_floor = mrkt_floor if mrkt_floor > 0 else sale_price
    all_markets = [
        ("mrkt", ref_floor),
        ("getgems", gg_floor),
        ("fragment", frag_floor),
        ("portal", portal_floor),
    ]
    all_markets = [(m, p) for m, p in all_markets if p > 0]
    if all_markets:
        all_markets.sort(key=lambda x: x[1], reverse=True)
        best_market, best_price = all_markets[0]
        # Prefer MRKT if it's within 5% of best (easier to list)
        if best_market != "mrkt" and ref_floor > 0 and best_price <= ref_floor * 1.05:
            best_market = "mrkt"
            best_price = int(ref_floor * 0.98)

    source = gift.get("_source", "mrkt")
    src_labels = {"mrkt": "🟦 MRKT", "getgems": "💎 Getgems", "portal": "🟣 Portal"}
    src_label = src_labels.get(source, source)

    text = f"🎁 <b>{name} #{number}</b>\n"
    text += f"📍 {src_label}\n"
    if model:
        text += f"Модель: {model}\n"
    if backdrop:
        text += f"Задник: {backdrop}\n"
    text += "\n"

    if on_sale:
        text += f"🏷 <b>На продаже:</b> {_ton(sale_price)} TON\n"
    else:
        text += "📦 <b>В хранилище</b>\n"

    text += "\n<b>Цены на маркетах:</b>\n"
    if mrkt_floor > 0:
        text += f"🟦 MRKT: {_ton(mrkt_floor)} TON\n"
    else:
        text += "🟦 MRKT: —\n"
    if gg_floor > 0:
        text += f"💎 Getgems: {_ton(gg_floor)} TON\n"
    else:
        text += "💎 Getgems: —\n"
    if portal_floor > 0:
        text += f"🟣 Portal: {_ton(portal_floor)} TON\n"
    else:
        text += "🟣 Portal: —\n"
    if frag_floor > 0:
        text += f"💜 Fragment: {_ton(frag_floor)} TON\n"
    else:
        text += "💜 Fragment: —\n"

    if best_market and best_price > 0 and not on_sale:
        market_labels = {
            "mrkt": "MRKT", "getgems": "Getgems",
            "fragment": "Fragment", "portal": "Portal",
        }
        market_name = market_labels.get(best_market, best_market)
        text += f"\n⚡ <b>Лучше продать на {market_name}</b>"

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_gift_detail_kb(gift_id, on_sale, best_market, best_price),
            )
        except Exception:
            pass
    await callback.answer()


# ── Sell Best (auto-recommendation) ────────────────────────────────────


@router.callback_query(F.data.startswith("sell_best:"))
async def cb_sell_best(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    gift_id = parts[1]
    market = parts[2]
    price = int(parts[3])

    if market == "mrkt":
        mrkt = _state["mrkt"]
        if not mrkt:
            await callback.answer("MRKT клиент не запущен")
            return

        result = await mrkt.sell_gift(gift_id, price)
        if result is not None:
            user_id = callback.from_user.id
            _user_inventory.pop(user_id, None)
            if callback.message:
                await callback.message.edit_text(
                    f"✅ <b>Выставлено на MRKT!</b>\n\nЦена: {_ton(price)} TON",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🎁 К инвентарю", callback_data="inv_refresh"
                                )
                            ],
                        ]
                    ),
                )
            await callback.answer("Выставлено!")
        else:
            await callback.answer("❌ Ошибка при выставлении", show_alert=True)

    elif market == "getgems":
        # Auto-withdraw from MRKT, then prompt for @gemsrelayer transfer
        mrkt = _state["mrkt"]
        if not mrkt:
            await callback.answer("MRKT клиент не запущен")
            return

        if callback.message:
            await callback.message.edit_text(
                "⏳ Вывожу подарок с MRKT для продажи на Getgems...",
                parse_mode="HTML",
            )

        result = await mrkt.withdraw_gift(gift_id)
        if result is None:
            if callback.message:
                await callback.message.edit_text(
                    "❌ Не удалось вывести подарок с MRKT.\n"
                    "Возможно, он на продаже — сначала снимите.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"gift:{gift_id}")],
                        ]
                    ),
                )
            await callback.answer()
            return

        user_id = callback.from_user.id
        _user_inventory.pop(user_id, None)

        # Find gift name for the callback
        gifts = _user_inventory.get(user_id, [])
        gift = next((g for g in gifts if g.get("id") == gift_id), None)
        name = gift.get("collectionName", "Подарок") if gift else "Подарок"

        global _gg_counter
        _gg_counter += 1
        pk = str(_gg_counter)
        _gg_pending[pk] = {"gift_id": gift_id, "name": name, "price": price}

        # Register with AutoSeller for background monitoring
        auto_seller = _state.get("auto_seller")
        if auto_seller and price > 0:
            auto_seller.add_pending_getgems(name, price)

        if callback.message:
            await callback.message.edit_text(
                f"✅ <b>Подарок выведен с MRKT!</b>\n\n"
                f"Рекомендуемая цена на Getgems: <b>{_ton(price)} TON</b>\n\n"
                f"Отправь подарок боту @gemsrelayer в Telegram.\n"
                f"Бот автоматически найдёт и выставит на продажу.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="✅ Отправил @gemsrelayer",
                                callback_data=f"ggt:{pk}",
                            )
                        ],
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
        await callback.answer("Выведен с MRKT!")


# ── Sell on MRKT ───────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("sell_mrkt:"))
async def cb_sell_mrkt(callback: CallbackQuery) -> None:
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]
    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])
    gift = next((g for g in gifts if g.get("id") == gift_id), None)

    name = gift.get("collectionName", "?") if gift else "?"

    # Get floor price for suggestions
    mds = _state["mds"]
    mrkt = _state["mrkt"]
    floor = 0
    if mds and mrkt:
        # Try MRKT floor
        try:
            listings = await mrkt.get_listings(
                collection_names=[name],
                count=1,
                ordering="Price",
                low_to_high=True,
            )
            first = (listings.get("gifts") or [None])[0]
            if first:
                floor = first.get("salePrice", 0) or 0
        except Exception:
            pass

        if floor == 0:
            floor = mds.get_cached_gg_floor(name)

    if floor <= 0:
        floor = 2_000_000_000  # fallback 2 TON

    text = f"💲 <b>Продать на MRKT</b>\n\n🎁 {name}\n\nВыбери цену:"

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_price_buttons(gift_id, floor, "mrkt"),
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("set_price_mrkt:"))
async def cb_set_price_mrkt(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")  # type: ignore[union-attr]
    gift_id = parts[1]
    price = int(parts[2])

    mrkt = _state["mrkt"]
    if not mrkt:
        await callback.answer("MRKT клиент не инициализирован")
        return

    result = await mrkt.sell_gift(gift_id, price)

    if result is not None:
        # Invalidate cache
        user_id = callback.from_user.id
        _user_inventory.pop(user_id, None)

        if callback.message:
            await callback.message.edit_text(
                f"✅ <b>Выставлено на MRKT!</b>\n\nЦена: {_ton(price)} TON",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
        await callback.answer("Выставлено!")
    else:
        await callback.answer("❌ Ошибка при выставлении", show_alert=True)


# ── Sell on Getgems ────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("sell_gg:"))
async def cb_sell_gg(callback: CallbackQuery) -> None:
    """Sell on Getgems: auto-withdraw from MRKT → prompt transfer → auto-list."""
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]

    mrkt = _state["mrkt"]
    if not mrkt:
        await callback.answer("MRKT клиент не запущен")
        return

    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])
    gift = next((g for g in gifts if g.get("id") == gift_id), None)
    name = gift.get("collectionName", "Подарок") if gift else "Подарок"
    number = gift.get("number", "") if gift else ""

    # Step 1: Auto-withdraw from MRKT
    if callback.message:
        await callback.message.edit_text(
            f"⏳ Вывожу <b>{name} #{number}</b> с MRKT...",
            parse_mode="HTML",
        )

    result = await mrkt.withdraw_gift(gift_id)
    if result is None:
        if callback.message:
            await callback.message.edit_text(
                f"❌ Не удалось вывести {name} #{number} с MRKT.\n"
                f"Возможно, подарок на продаже — сначала снимите.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"gift:{gift_id}")],
                    ]
                ),
            )
        await callback.answer()
        return

    _user_inventory.pop(user_id, None)

    # Step 2: Prompt user to send to @gemsrelayer
    # Get recommended price
    mds = _state["mds"]
    gg_floor = 0
    if mds:
        gg_floor = mds.get_cached_gg_floor(name)
    rec_price = int(gg_floor * 0.98) if gg_floor > 0 else 0

    price_text = f"\nРекомендуемая цена: <b>{_ton(rec_price)} TON</b>" if rec_price > 0 else ""

    # Store pending transfer with short key (Telegram limit: 64 bytes)
    global _gg_counter
    _gg_counter += 1
    pk = str(_gg_counter)
    _gg_pending[pk] = {"gift_id": gift_id, "name": name, "price": rec_price}

    # Register with AutoSeller for background monitoring
    auto_seller = _state.get("auto_seller")
    if auto_seller and rec_price > 0:
        auto_seller.add_pending_getgems(name, rec_price, str(number))

    if callback.message:
        await callback.message.edit_text(
            f"✅ <b>{name} #{number} выведен с MRKT!</b>\n\n"
            f"Теперь отправь этот подарок боту @gemsrelayer в Telegram.\n"
            f"Подарок появится на Getgems.{price_text}\n\n"
            f"Бот автоматически найдёт и выставит на продажу.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Отправил @gemsrelayer",
                            callback_data=f"ggt:{pk}",
                        )
                    ],
                    [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                ]
            ),
        )
    await callback.answer("Выведен с MRKT!")


@router.callback_query(F.data.startswith("withdraw:"))
async def cb_withdraw(callback: CallbackQuery) -> None:
    """Withdraw gift from MRKT back to Telegram."""
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]

    mrkt = _state["mrkt"]
    if not mrkt:
        await callback.answer("MRKT клиент не запущен")
        return

    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])
    gift = next((g for g in gifts if g.get("id") == gift_id), None)
    name = gift.get("collectionName", "Подарок") if gift else "Подарок"
    number = gift.get("number", "") if gift else ""

    if callback.message:
        await callback.message.edit_text(
            f"⏳ Вывожу <b>{name} #{number}</b> с MRKT...",
            parse_mode="HTML",
        )

    result = await mrkt.withdraw_gift(gift_id)
    if result is not None:
        _user_inventory.pop(user_id, None)
        if callback.message:
            await callback.message.edit_text(
                f"✅ <b>{name} #{number} выведен с MRKT!</b>\n\nПодарок вернулся в Telegram.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
        await callback.answer("Выведен!")
    else:
        if callback.message:
            await callback.message.edit_text(
                f"❌ Не удалось вывести {name} #{number}.\n"
                f"Возможно, подарок на продаже — сначала снимите.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"gift:{gift_id}")],
                    ]
                ),
            )
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("ggt:"))
async def cb_gg_transferred(callback: CallbackQuery) -> None:
    """User confirmed they sent the gift to @gemsrelayer. Auto-list on Getgems."""
    pk = (callback.data or "").split(":")[1] if ":" in (callback.data or "") else ""
    pending = _gg_pending.get(pk, {})
    name = pending.get("name", "Подарок")
    rec_price = pending.get("price", 0)

    gg = _state.get("gg")
    if not gg or not gg._keypair or not gg._wallet_address:
        if callback.message:
            await callback.message.edit_text(
                "✅ Подарок отправлен на Getgems!\n\n"
                "Для автоматической продажи подключи кошелёк:\n"
                "<code>/wallet слово1 слово2 ... слово24</code>\n\n"
                "Пока кошелёк не подключён, выстави вручную на getgems.io",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🌐 Getgems", url="https://getgems.io/user/gifts"
                            )
                        ],
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
        await callback.answer()
        return

    # Try to find the gift on Getgems and auto-list
    if callback.message:
        await callback.message.edit_text(
            f"⏳ Ищу <b>{name}</b> на Getgems и выставляю на продажу...",
            parse_mode="HTML",
        )

    # Try to find gift NFT address via Getgems user inventory
    listed = False
    try:
        user_gifts = await gg.get_user_offchain_gifts(gg._wallet_address)
        for g in user_gifts:
            g_name = g.get("name", "")
            if name.lower() in g_name.lower() or g_name.lower() in name.lower():
                nft_addr = g.get("address", "")
                if nft_addr and rec_price > 0:
                    success = await gg.list_offchain_gift_rest(nft_addr, rec_price)
                    if success:
                        listed = True
                        break
    except Exception as e:
        logger.error("Getgems auto-list after transfer failed: %s", e)

    if listed:
        _gg_pending.pop(pk, None)
        if callback.message:
            await callback.message.edit_text(
                f"✅ <b>{name} выставлен на Getgems!</b>\n\n"
                f"Цена: <b>{_ton(rec_price)} TON</b>\n"
                f"Продажа автоматическая.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🌐 Getgems", url="https://getgems.io/user/gifts"
                            )
                        ],
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
    else:
        if callback.message:
            await callback.message.edit_text(
                f"⚠️ Не удалось автоматически выставить {name}.\n\n"
                f"Подарок может ещё не появиться на Getgems (подожди 1-2 минуты).\n"
                f"Попробуй снова или выстави вручную.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=f"ggt:{pk}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🌐 Getgems", url="https://getgems.io/user/gifts"
                            )
                        ],
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
    await callback.answer()


# ── Reprice / Cancel Sale ──────────────────────────────────────────────


@router.callback_query(F.data.startswith("reprice:"))
async def cb_reprice(callback: CallbackQuery) -> None:
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]
    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])
    gift = next((g for g in gifts if g.get("id") == gift_id), None)

    current_price = gift.get("salePrice", 0) if gift else 0
    floor = current_price or 2_000_000_000

    text = f"💲 <b>Изменить цену</b>\n\nТекущая: {_ton(current_price)} TON\n\nВыбери новую:"

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=_price_buttons(gift_id, floor, "mrkt"),
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("unsell:"))
async def cb_unsell(callback: CallbackQuery) -> None:
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]

    mrkt = _state["mrkt"]
    if not mrkt:
        await callback.answer("MRKT клиент не инициализирован")
        return

    result = await mrkt.cancel_sale(gift_id)
    if result is not None:
        user_id = callback.from_user.id
        _user_inventory.pop(user_id, None)

        if callback.message:
            await callback.message.edit_text(
                "✅ <b>Снято с продажи</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🎁 К инвентарю", callback_data="inv_refresh")],
                    ]
                ),
            )
        await callback.answer("Снято!")
    else:
        await callback.answer("❌ Ошибка", show_alert=True)


# ── Compare Prices ─────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("compare:"))
async def cb_compare(callback: CallbackQuery) -> None:
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]
    user_id = callback.from_user.id
    gifts = _user_inventory.get(user_id, [])
    gift = next((g for g in gifts if g.get("id") == gift_id), None)
    name = gift.get("collectionName", "?") if gift else "?"

    mds = _state["mds"]
    mrkt = _state["mrkt"]

    # MRKT floor
    mrkt_floor = 0
    if mrkt:
        try:
            listings = await mrkt.get_listings(
                collection_names=[name],
                count=1,
                ordering="Price",
                low_to_high=True,
            )
            first = (listings.get("gifts") or [None])[0]
            if first:
                mrkt_floor = first.get("salePrice", 0) or 0
        except Exception:
            pass

    # Getgems floor
    gg_floor = mds.get_cached_gg_floor(name) if mds else 0
    # Fragment floor
    frag_floor = mds.get_cached_fragment_floor(name) if mds else 0

    text = f"📊 <b>Сравнение цен: {name}</b>\n\n"
    text += f"🟦 MRKT floor: {_ton(mrkt_floor)} TON\n" if mrkt_floor else "🟦 MRKT floor: н/д\n"
    text += f"💎 Getgems floor: {_ton(gg_floor)} TON\n" if gg_floor else "💎 Getgems floor: н/д\n"
    text += (
        f"💜 Fragment floor: {_ton(frag_floor)} TON\n" if frag_floor else "💜 Fragment floor: н/д\n"
    )

    # Find best sell market
    prices = [("MRKT", mrkt_floor), ("Getgems", gg_floor), ("Fragment", frag_floor)]
    prices = [(m, p) for m, p in prices if p > 0]
    if len(prices) >= 2:
        prices.sort(key=lambda x: x[1], reverse=True)
        best_market, best_price = prices[0]
        worst_market, worst_price = prices[-1]
        spread = ((best_price - worst_price) / worst_price) * 100
        text += f"\n📈 {best_market} дороже на {spread:.1f}%"
        text += f"\n💡 Выгоднее продавать на {best_market}"

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"gift:{gift_id}")],
                    ]
                ),
            )
        except Exception:
            pass
    await callback.answer()


# ── Status ─────────────────────────────────────────────────────────────


async def _status_text() -> str:
    import datetime as _dt

    start_ts = _state.get("start_time", 0)
    start_str = (
        _dt.datetime.fromtimestamp(start_ts).strftime("%d.%m.%Y %H:%M:%S") if start_ts else "н/д"
    )
    uptime_sec = int(time.time() - start_ts) if start_ts else 0
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}ч {mins}м" if hours else f"{mins}м {secs}с"

    parts: list[str] = [
        f"📊 <b>Статус системы</b>\n🕐 Старт: {start_str}\n⏱ Аптайм: {uptime_str}\n"
    ]

    # System health indicators
    mrkt = _state.get("mrkt")
    gg = _state.get("gg")
    mds = _state.get("mds")
    gg_floors_count = len(mds._gg_floors) if mds else 0
    frag_floors_count = len(mds._fragment_floors) if mds else 0
    portal_floors_count = len(mds._portal_floors) if mds else 0
    frag_client = mds._fragment if mds else None
    portal_client = mds._portal if mds else None
    parts.append(
        f"{'✅' if mrkt and mrkt._auth_token else '❌'} MRKT токен\n"
        f"{'✅' if gg and gg._wallet_address else '❌'} Getgems кошелёк\n"
        f"{'✅' if gg and gg._user_token else '❌'} Getgems авторизация\n"
        f"{'✅' if gg_floors_count > 0 else '❌'} Getgems цены: {gg_floors_count}\n"
        f"{'✅' if frag_client and frag_client.ready else '❌'} Fragment: {frag_floors_count} цен\n"
        f"{'✅' if portal_floors_count > 0 else '❌'} Portal: {portal_floors_count} цен"
        f"{' (auth)' if portal_client and portal_client.authenticated else ''}\n"
    )

    scanner = _state["scanner"]
    if scanner:
        s = scanner.get_status()
        scan_label = (
            "на паузе" if s.get("paused") else ("работает" if s["running"] else "остановлен")
        )
        scan_icon = "⏸" if s.get("paused") else ("🟢" if s["running"] else "🔴")
        markets = s.get("markets_enabled", {})
        m_parts = []
        for k, v in markets.items():
            if isinstance(v, dict):
                b = "🟢" if v.get("buy", True) else "🔴"
                s_icon = "🟢" if v.get("sell", True) else "🔴"
                m_parts.append(f"{k.upper()}({b}B {s_icon}S)")
            else:
                m_parts.append(f"{'🟢' if v else '🔴'}{k.upper()}")
        m_str = " | ".join(m_parts)
        parts.append(
            f"{scan_icon} Сканер: {scan_label}\n"
            f"👻 Теневой режим: {'ВКЛ' if s['shadow_mode'] else 'ВЫКЛ'}\n"
            f"🏪 Маркеты: {m_str}\n"
            f"📂 Коллекций: {s['total_collections']}\n"
            f"✅ Просканировано: {s.get('scanned_collections', 0)}\n"
            f"🔄 Циклов: {s['cycles']}\n"
            f"📡 Bulk-сканов: {s.get('bulk_scans', 0)}\n"
            f"🔥 Перспективных: {s.get('promising_collections', 0)}\n"
            f"🎯 Найдено: {s['opportunities_found']}\n"
            f"⚠️ Ошибок: {s.get('errors', 0)}"
        )
        if s.get("last_scan_time"):
            import datetime as _dt

            scan_time = _dt.datetime.fromtimestamp(s["last_scan_time"]).strftime("%H:%M:%S")
            parts.append(f"\n🔍 Последний скан: {s['last_scan']} в {scan_time}")
        elif s.get("last_scan"):
            parts.append(f"\n🔍 Последний скан: {s['last_scan']}")

    # Auto-seller status
    auto_seller = _state.get("auto_seller")
    if auto_seller:
        ast = auto_seller.get_status()
        parts.append(
            f"\n🤖 Автопродажа: {'🟢 ВКЛ' if ast['enabled'] else '🔴 ВЫКЛ'}\n"
            f"   Выставлено: {ast['stats']['listed']}\n"
            f"   Переоценено: {ast['stats']['repriced']}"
        )

    circuit = _state["circuit"]
    if circuit:
        cs = circuit.get_status()
        status = "🔴 OPEN" if cs["global_open"] else "🟢 OK"
        parts.append(f"\n⚡ Автомат: {status}\n🚫 Заблокировано: {cs['blocked_count']} коллекций")

    mds = _state["mds"]
    if mds:
        gg_count = len(mds._gg_floors)
        frag_count = len(mds._fragment_floors)
        portal_count = len(mds._portal_floors)
        parts.append(f"\n💎 Цен Getgems: {gg_count}")
        parts.append(f"💜 Цен Fragment: {frag_count}")
        parts.append(f"🟣 Цен Portal: {portal_count}")

    # Gift transfer status
    gt = _state.get("gift_transfer")
    if gt:
        gts = gt.get_status()
        gt_icon = "🟢" if gts["connected"] else "🔴"
        parts.append(
            f"\n🎁 Передача подарков: {gt_icon} {'Подключён' if gts['connected'] else 'Отключён'}"
        )
        if gts["connected"]:
            parts.append(f"   👤 {gts['user']}")
            targets = gts.get("targets", {})
            if targets:
                t_lines = []
                for m, u in targets.items():
                    if u:
                        t_lines.append(f"{m.upper()}→@{u}")
                if t_lines:
                    parts.append(f"   📤 {' | '.join(t_lines)}")
            stats = gts.get("stats", {})
            parts.append(
                f"   📊 Передано: {stats.get('transfers', 0)} | "
                f"На холде: {gts.get('pending_holds', 0)}"
            )

    # Balances
    parts.append("")
    mrkt = _state.get("mrkt")
    logger.debug("Status: mrkt client = %s", type(mrkt))
    if mrkt:
        try:
            bal = await mrkt.get_balance()
            logger.debug("Status: mrkt balance response = %s", bal)
            if bal and bal.get("hard") is not None:
                mrkt_ton = bal["hard"] / 1_000_000_000
                parts.append(f"🟦 Баланс MRKT: <b>{mrkt_ton:.2f}</b> TON")
            elif bal:
                parts.append(f"🟦 Баланс MRKT: <i>формат ответа: {list(bal.keys())[:5]}</i>")
            else:
                parts.append("🟦 Баланс MRKT: <i>токен истёк</i>")
        except Exception as e:
            logger.exception("MRKT balance error")
            parts.append(f"🟦 Баланс MRKT: <i>ошибка ({e})</i>")
    else:
        parts.append("🟦 Баланс MRKT: <i>клиент не подключён</i>")
    gg = _state.get("gg")
    if gg and gg._wallet_address:
        try:
            balance = await gg.get_wallet_balance()
            parts.append(f"💰 Баланс кошелька: <b>{balance / 1_000_000_000:.2f}</b> TON")
        except Exception:
            pass

    # Portal balance
    portal_c = mds._portal if mds and hasattr(mds, "_portal") else None
    if portal_c and portal_c.authenticated:
        try:
            portal_bal = await portal_c.get_balance()
            if portal_bal is not None:
                parts.append(f"🟣 Баланс Portal: <b>{portal_bal:.2f}</b> TON")
        except Exception:
            pass

    # Auto-seller reverse stats
    if auto_seller:
        ast2 = auto_seller.get_status()
        gg_buys = ast2["stats"].get("gg_buys", 0)
        gg_shadow = ast2["stats"].get("gg_buy_shadow", 0)
        if gg_buys or gg_shadow:
            parts.append(f"\n🔄 Getgems→MRKT: куплено {gg_buys}, шедоу {gg_shadow}")
        mrkt_pend = ast2.get("mrkt_pending", {})
        if mrkt_pend:
            parts.append(f"📦 Ожидают перевода на MRKT: {len(mrkt_pend)}")

    active = await state_machine.get_active_deals()
    parts.append(f"\n📋 Сделок: {len(active)}")

    return "\n".join(parts)


@router.message(Command("status"))
async def cmd_status(message: types.Message) -> None:
    await message.answer(await _status_text(), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(F.text == "📊 Статус")
async def btn_status(message: types.Message) -> None:
    await message.answer(await _status_text(), parse_mode="HTML", reply_markup=MAIN_KB)


# ── Statistics ─────────────────────────────────────────────────────────


def _format_hours(h: float) -> str:
    if h < 1:
        return f"{int(h * 60)}мин"
    if h < 24:
        return f"{h:.1f}ч"
    return f"{h / 24:.1f}д"


async def _stats_text(days: int = 30) -> str:
    stats_svc = _state.get("statistics")
    if not stats_svc:
        return "📉 Статистика не инициализирована"

    s = await stats_svc.get_full_stats(days=days)

    period_label = {1: "сегодня", 7: "за неделю", 30: "за 30 дней"}.get(days, f"за {days}д")
    parts: list[str] = [f"📉 <b>СТАТИСТИКА</b> ({period_label})\n"]

    # Overall P&L
    profit = s["total_profit_ton"]
    parts.append("<b>💰 Общий P&L</b>")
    if s["initial_balance_ton"] > 0:
        parts.append(f"├─ Начальный: {s['initial_balance_ton']:.1f} TON ({s['initial_date']})")
    if s["latest_total_ton"] > 0:
        parts.append(f"├─ Текущий: {s['latest_total_ton']:.1f} TON")
    parts.append(f"├─ В подарках: {s['active_exposure_ton']:.1f} TON ({s['active_items']} шт.)")
    parts.append(f"├─ Unrealized: {s['unrealized_pnl_ton']:+.1f} TON")
    parts.append(f"└─ <b>Чистая прибыль: {profit:+.2f} TON</b>")

    # Deals
    parts.append("\n<b>📈 Сделки</b>")
    parts.append(
        f"├─ Всего: {s['total_deals']} (авто: {s['auto_deals']}, ручных: {s['manual_deals']})"
    )
    if s["auto_deals"] > 0:
        parts.append(f"├─ Успешных: {s['winners']}/{s['auto_deals']} ({s['win_rate_pct']:.0f}%)")
        parts.append(f"├─ Средний ROI: {s['avg_roi_pct']:.1f}%")
    if s["best_deal"]:
        bd = s["best_deal"]
        parts.append(
            f"├─ Лучшая: {bd['collection']} {bd['profit_ton']:+.1f} TON ({bd['roi_pct']:.0f}%)"
        )
    if s["worst_deal"]:
        wd = s["worst_deal"]
        parts.append(
            f"└─ Худшая: {wd['collection']} {wd['profit_ton']:+.1f} TON ({wd['roi_pct']:.0f}%)"
        )

    # By route
    if s["by_route"]:
        parts.append("\n<b>🏪 По маркетам</b>")
        for route, info in sorted(
            s["by_route"].items(), key=lambda x: x[1]["profit_ton"], reverse=True
        ):
            parts.append(f"├─ {route}: {info['deals']} сд., {info['profit_ton']:+.1f} TON")
    if s["manual_deals"] > 0:
        parts.append(f"└─ Ручные: {s['manual_deals']} сд., {s['manual_profit_ton']:+.1f} TON")

    # Fees
    if s["total_fees_ton"] > 0:
        parts.append(f"\n<b>💸 Комиссии:</b> -{s['total_fees_ton']:.2f} TON")

    # Speed
    if s["avg_sell_hours"] > 0:
        parts.append("\n<b>⏱ Скорость</b>")
        parts.append(f"├─ Средняя: {_format_hours(s['avg_sell_hours'])}")
        parts.append(f"├─ Быстрая: {_format_hours(s['min_sell_hours'])}")
        parts.append(f"└─ Долгая: {_format_hours(s['max_sell_hours'])}")

    return "\n".join(parts)


async def _portfolio_text() -> str:
    stats_svc = _state.get("statistics")
    mds = _state.get("mds")
    if not stats_svc or not mds:
        return "💼 Портфель не инициализирован"

    # Get active deals
    from sqlalchemy import select

    from bot.models.database import Deal, async_session
    from bot.models.types import TERMINAL_STATES, DealState

    active_states = [s.value for s in DealState if s not in TERMINAL_STATES]
    async with async_session() as session:
        result = await session.execute(
            select(Deal)
            .where(
                Deal.state.in_(active_states),
                Deal.is_shadow.is_(False),
            )
            .order_by(Deal.detected_at.desc())
        )
        deals = result.scalars().all()

    if not deals:
        return "💼 <b>ПОРТФЕЛЬ</b>\n\n📦 Нет активных подарков"

    mrkt_floors = mds.get_all_mrkt_floors()
    gg_floors = mds.get_all_gg_floors()
    frag_floors = mds.get_all_fragment_floors()
    portal_floors = mds.get_all_portal_floors()

    # Group by collection
    by_coll: dict[str, list] = {}
    for d in deals:
        coll = d.collection_name
        if coll not in by_coll:
            by_coll[coll] = []
        by_coll[coll].append(d)

    parts: list[str] = [f"💼 <b>ПОРТФЕЛЬ</b> ({len(deals)} подарков)\n"]
    total_cost = 0
    total_unrealized = 0

    for coll, coll_deals in sorted(by_coll.items()):
        count = len(coll_deals)
        cost = sum(d.buy_price for d in coll_deals)
        total_cost += cost

        # Get floors
        mrkt_fl = mrkt_floors.get(coll, 0)
        gg_fl = 0
        for k, v in gg_floors.items():
            if k.lower() == coll.lower() or coll.lower() in k.lower():
                gg_fl = v
                break
        frag_fl = frag_floors.get(coll, 0)
        portal_fl = portal_floors.get(coll, 0)
        best_fl = max(mrkt_fl, gg_fl, frag_fl, portal_fl)

        unrealized = 0
        if best_fl > 0:
            unrealized = int(best_fl * 0.95) * count - cost
        total_unrealized += unrealized

        parts.append(f"💎 <b>{coll}</b> ×{count} — {cost / 1e9:.1f} TON")

        floor_parts = []
        if mrkt_fl > 0:
            floor_parts.append(f"M:{mrkt_fl / 1e9:.1f}")
        if gg_fl > 0:
            floor_parts.append(f"GG:{gg_fl / 1e9:.1f}")
        if frag_fl > 0:
            floor_parts.append(f"Fr:{frag_fl / 1e9:.1f}")
        if portal_fl > 0:
            floor_parts.append(f"Pt:{portal_fl / 1e9:.1f}")
        if floor_parts:
            parts.append(f"  Floor: {' / '.join(floor_parts)}")

        if unrealized != 0:
            pct = (unrealized / cost * 100) if cost > 0 else 0
            parts.append(f"  Unrealized: {unrealized / 1e9:+.1f} TON ({pct:+.0f}%)")

        # Age
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        avg_age = sum((now - d.detected_at).total_seconds() / 3600 for d in coll_deals) / count
        parts.append(f"  Возраст: {_format_hours(avg_age)}")
        parts.append("")

    pct = (total_unrealized / total_cost * 100) if total_cost > 0 else 0
    parts.append(f"<b>💰 Экспозиция:</b> {total_cost / 1e9:.1f} TON")
    parts.append(f"<b>📊 Unrealized P&L:</b> {total_unrealized / 1e9:+.1f} TON ({pct:+.0f}%)")

    return "\n".join(parts)


@router.message(Command("stats"))
async def cmd_stats(message: types.Message) -> None:
    args = message.text.strip().split() if message.text else []
    days = 30
    if len(args) > 1:
        arg = args[1].lower()
        if arg in ("day", "today", "сегодня", "день"):
            days = 1
        elif arg in ("week", "неделя"):
            days = 7
        elif arg in ("month", "месяц"):
            days = 30
        elif arg.isdigit():
            days = int(arg)
    await message.answer(await _stats_text(days), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(F.text == "📉 Статистика")
async def btn_stats(message: types.Message) -> None:
    # Show period selection
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="stats_period:1"),
                InlineKeyboardButton(text="7 дней", callback_data="stats_period:7"),
                InlineKeyboardButton(text="30 дней", callback_data="stats_period:30"),
            ],
            [
                InlineKeyboardButton(text="💼 Портфель", callback_data="stats_portfolio"),
            ],
        ]
    )
    await message.answer("📉 Выбери период:", reply_markup=kb)


@router.callback_query(F.data.startswith("stats_period:"))
async def cb_stats_period(callback: CallbackQuery) -> None:
    days = int(callback.data.split(":")[1])  # type: ignore[union-attr]
    text = await _stats_text(days)
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "stats_portfolio")
async def cb_stats_portfolio(callback: CallbackQuery) -> None:
    text = await _portfolio_text()
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.message(Command("portfolio"))
async def cmd_portfolio(message: types.Message) -> None:
    await message.answer(await _portfolio_text(), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(F.text == "💰 Портфель")
async def btn_portfolio(message: types.Message) -> None:
    await message.answer(await _portfolio_text(), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(Command("sold"))
async def cmd_sold(message: types.Message) -> None:
    """Register a manual sale: /sold "Genie Lamp" 38.5 [mrkt|getgems]"""
    stats_svc = _state.get("statistics")
    if not stats_svc:
        await message.answer("Статистика не инициализирована")
        return

    args = message.text.strip() if message.text else ""
    # Parse: /sold "Name" price [market]
    import re

    match = re.match(r'/sold\s+"([^"]+)"\s+([\d.]+)\s*(\w+)?', args)
    if not match:
        match = re.match(r"/sold\s+(\S+)\s+([\d.]+)\s*(\w+)?", args)
    if not match:
        await message.answer(
            'Формат: /sold "Genie Lamp" 38.5 [mrkt|getgems]\nИли: /sold GenieLamp 38.5 mrkt',
            parse_mode="HTML",
        )
        return

    name = match.group(1)
    price_ton = float(match.group(2))
    market = (match.group(3) or "mrkt").lower()
    price_nano = int(price_ton * 1e9)

    trade = await stats_svc.record_manual_trade(
        gift_name=name,
        collection=name,
        action="sell",
        market=market,
        price_nanoton=price_nano,
        note="manual /sold command",
    )

    fee_ton = trade.fee_nanoton / 1e9
    net = price_ton - fee_ton
    await message.answer(
        f"✅ Записано: <b>{name}</b> продан за {price_ton:.2f} TON\n"
        f"💸 Комиссия {market}: {fee_ton:.2f} TON\n"
        f"💰 Чистыми: {net:.2f} TON",
        parse_mode="HTML",
    )


# ── Market ─────────────────────────────────────────────────────────────


async def _market_text() -> str:
    mds = _state["mds"]
    if not mds:
        return "📈 Данные рынка не загружены"

    floors = mds._gg_floors
    if not floors:
        return "📈 <b>Рынок</b>\n\nЦены Getgems пока не загружены"

    lines: list[str] = ["📈 <b>Топ коллекций (минимальная цена Getgems)</b>\n"]
    sorted_floors = sorted(floors.items(), key=lambda x: x[1])
    for name, price_nano in sorted_floors[:20]:
        lines.append(f"  {name}: <b>{_ton(price_nano)}</b> TON")

    lines.append(f"\nВсего: {len(floors)} коллекций")
    return "\n".join(lines)


@router.message(Command("market"))
async def cmd_market(message: types.Message) -> None:
    await message.answer(await _market_text(), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(F.text == "📈 Рынок")
async def btn_market(message: types.Message) -> None:
    await message.answer(await _market_text(), parse_mode="HTML", reply_markup=MAIN_KB)


# ── Risk ───────────────────────────────────────────────────────────────


async def _risk_text() -> str:
    risk = _state["risk"]
    if not risk:
        return "🛡 Управление рисками не запущено"

    status = await risk.get_status()
    return (
        f"🛡 <b>Risk Engine</b>\n\n"
        f"🔒 Ужесточение: {status['tightening_factor']:.0%}\n"
        f"💰 Экспозиция: {status['total_exposure_ton']:.2f} TON\n"
        f"📦 Предметов: {status['total_items']}\n"
        f"⏰ Зависших: {status['stuck_items']}\n"
        f"📉 Потери за день: {status['daily_losses_ton']:.2f} TON"
    )


@router.message(Command("risk"))
async def cmd_risk(message: types.Message) -> None:
    await message.answer(await _risk_text(), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(F.text == "🛡 Риски")
async def btn_risk(message: types.Message) -> None:
    await message.answer(await _risk_text(), parse_mode="HTML", reply_markup=MAIN_KB)


# ── Settings ───────────────────────────────────────────────────────────


@router.message(Command("shadow"))
async def cmd_shadow(message: types.Message) -> None:
    scanner = _state["scanner"]
    if scanner:
        scanner.shadow_mode = not scanner.shadow_mode
        # Sync shadow mode to auto_seller and execution engine
        auto_seller = _state.get("auto_seller")
        if auto_seller:
            auto_seller._shadow_mode = scanner.shadow_mode
        execution = _state.get("execution")
        if execution:
            execution.shadow_mode = scanner.shadow_mode
        mode = "ON 👻" if scanner.shadow_mode else "OFF ⚠️"
        await message.answer(
            f"Теневой режим: <b>{mode}</b>", parse_mode="HTML", reply_markup=MAIN_KB
        )
    else:
        await message.answer("Сканер не запущен", reply_markup=MAIN_KB)


@router.message(F.text == "⚙️ Настройки")
async def btn_settings(message: types.Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    _waiting_setting.pop(uid, None)  # reset pending selection

    scanner = _state["scanner"]
    shadow = "ВКЛ 👻" if (scanner and scanner.shadow_mode) else "ВЫКЛ ⚠️"
    running = "🟢 Работает" if (scanner and scanner._running) else "🔴 Остановлен"
    auto_seller = _state.get("auto_seller")
    auto_sell_status = "🟢 ВКЛ" if (auto_seller and auto_seller.enabled) else "🔴 ВЫКЛ"

    # Market status icons (buy/sell separate)
    def _mkt_line(name: str, key: str) -> str:
        e = _market_enabled.get(key, {"buy": True, "sell": True})
        b = "🟢" if e.get("buy", True) else "🔴"
        s = "🟢" if e.get("sell", True) else "🔴"
        return f"  {name}: {b}покупка {s}продажа"

    lines = [
        "⚙️ <b>Настройки</b>\n",
        f"Сканер: {running}",
        f"Теневой режим: {shadow}",
        f"Автопродажа: {auto_sell_status}\n",
        "🏪 <b>Маркеты (покупка/продажа):</b>",
        _mkt_line("MRKT", "mrkt"),
        _mkt_line("Getgems", "getgems"),
        _mkt_line("Fragment", "fragment"),
        _mkt_line("Portal", "portal"),
        "",
        "📊 <b>Цена продажи:</b>",
        f"  MRKT: {'мгновенная' if _sell_price_mode.get('mrkt') == 'instant' else 'floor'}",
        f"  Portal: {'мгновенная' if _sell_price_mode.get('portal') == 'instant' else 'floor'}",
        "",
        "📊 <b>Параметры (напиши номер чтобы изменить):</b>\n",
    ]
    for i, key in enumerate(_SETTING_KEYS, 1):
        label = _SETTING_LABELS.get(key, key)
        val = _runtime[key]
        lines.append(f"  <b>{i}.</b> {label}: <b>{val:g}</b>")

    lines.append("\n👻 <b>0.</b> Теневой режим вкл/выкл")
    lines.append("\n<i>Напиши номер (0-{}) в чат:</i>".format(len(_SETTING_KEYS)))

    from bot.config import settings as _cfg

    fp_icon = "🟢" if _cfg.falling_price_enabled else "🔴"
    fp_interval = {
        300_000: "5м", 600_000: "10м", 1_800_000: "30м", 3_600_000: "1ч",
        10_800_000: "3ч", 21_600_000: "6ч", 43_200_000: "12ч",
        86_400_000: "1д", 172_800_000: "2д",
    }.get(_cfg.falling_price_interval_ms, "?")

    lines.append(
        f"\n📉 <b>Падающая цена:</b> {fp_icon} "
        f"(-{_cfg.falling_price_decrease_pct:.0f}% каждые {fp_interval})"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👻 Shadow", callback_data="stg:shadow"),
                InlineKeyboardButton(text="🤖 Автопродажа", callback_data="stg:autosell"),
            ],
            [
                InlineKeyboardButton(text="⏸ Пауза сканера", callback_data="stg:pause"),
                InlineKeyboardButton(
                    text=f"{fp_icon} Падающая цена",
                    callback_data="stg:falling_price",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['mrkt']['buy'] else '🔴'} MRKT покупка",
                    callback_data="stg:mkt:mrkt:buy",
                ),
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['mrkt']['sell'] else '🔴'} MRKT продажа",
                    callback_data="stg:mkt:mrkt:sell",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['getgems']['buy'] else '🔴'} GG покупка",
                    callback_data="stg:mkt:getgems:buy",
                ),
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['getgems']['sell'] else '🔴'} GG продажа",
                    callback_data="stg:mkt:getgems:sell",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['fragment']['buy'] else '🔴'} Frag покупка",
                    callback_data="stg:mkt:fragment:buy",
                ),
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['fragment']['sell'] else '🔴'} Frag продажа",
                    callback_data="stg:mkt:fragment:sell",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['portal']['buy'] else '🔴'} Portal покупка",
                    callback_data="stg:mkt:portal:buy",
                ),
                InlineKeyboardButton(
                    text=f"{'🟢' if _market_enabled['portal']['sell'] else '🔴'} Portal продажа",
                    callback_data="stg:mkt:portal:sell",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=(
                        "📊 MRKT: "
                        + ("мгновенная" if _sell_price_mode.get("mrkt") == "instant" else "floor")
                    ),
                    callback_data="stg:sellmode:mrkt",
                ),
                InlineKeyboardButton(
                    text=(
                        "📊 Portal: "
                        + ("мгновенная" if _sell_price_mode.get("portal") == "instant" else "floor")
                    ),
                    callback_data="stg:sellmode:portal",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Доступы", callback_data="stg:auth"
                ),
            ],
        ]
    )

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


# ── Apply setting helper ───────────────────────────────────────────────


def _apply_setting(key: str, value: float) -> None:
    """Apply a runtime setting and propagate to relevant components."""
    _runtime[key] = value

    auto_seller = _state.get("auto_seller")
    _state.get("scanner")
    risk = _state.get("risk")
    _state.get("execution")

    if key == "min_roi_pct":
        if auto_seller:
            auto_seller._min_profit_pct = value
        from bot.intelligence.opportunity_scorer import set_runtime_thresholds

        set_runtime_thresholds(min_roi_pct=value)

    if key == "scan_interval" and auto_seller:
        auto_seller._check_interval = value

    if key == "autosell_interval" and auto_seller:
        auto_seller._check_interval = value

    if key == "max_exposure_ton" and risk:
        risk._max_total = int(value * 1_000_000_000)

    logger.info("Setting %s = %s", key, value)


# ── Settings callbacks ─────────────────────────────────────────────────

_SETTING_LABELS: dict[str, str] = {
    "min_roi_pct": "Мин. ROI покупка (%)",
    "cross_roi_pct": "Мин. ROI кросс-маркет (%)",
    "order_roi_pct": "Мин. ROI ордер-арб (%)",
    "deep_roi_pct": "Мин. ROI скидка (%)",
    "scan_interval": "Интервал скана (сек)",
    "max_buy_ton": "Макс. покупка (TON)",
    "max_exposure_ton": "Макс. экспозиция (TON)",
    "autosell_interval": "Интервал автопродажи (сек)",
}

_SETTING_OPTIONS: dict[str, list[float]] = {
    "min_roi_pct": [3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0],
    "cross_roi_pct": [3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0],
    "order_roi_pct": [3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0],
    "deep_roi_pct": [3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0],
    "scan_interval": [30, 60, 90, 120, 180, 300],
    "max_buy_ton": [10, 20, 30, 50, 60, 100, 200],
    "max_exposure_ton": [50, 100, 200, 300, 600],
    "autosell_interval": [60, 90, 120, 180, 300],
}


@router.callback_query(F.data == "stg:shadow")
async def stg_shadow(callback: CallbackQuery) -> None:
    scanner = _state["scanner"]
    if scanner:
        scanner.shadow_mode = not scanner.shadow_mode
        auto_seller = _state.get("auto_seller")
        if auto_seller:
            auto_seller._shadow_mode = scanner.shadow_mode
        execution = _state.get("execution")
        if execution:
            execution.shadow_mode = scanner.shadow_mode
    mode = "ВКЛ 👻" if (scanner and scanner.shadow_mode) else "ВЫКЛ ⚠️"
    await callback.answer(f"Теневой режим: {mode}")
    if callback.message:
        await callback.message.edit_text(
            f"👻 Теневой режим: <b>{mode}</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "stg:autosell")
async def stg_autosell(callback: CallbackQuery) -> None:
    auto_seller = _state.get("auto_seller")
    if auto_seller:
        auto_seller.enabled = not auto_seller.enabled
    mode = "ВКЛ 🟢" if (auto_seller and auto_seller.enabled) else "ВЫКЛ 🔴"
    await callback.answer(f"Автопродажа: {mode}")
    if callback.message:
        await callback.message.edit_text(
            f"🤖 Автопродажа: <b>{mode}</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("stg:market:"))
async def stg_market_toggle(callback: CallbackQuery) -> None:
    """Legacy handler — toggles both buy and sell."""
    market = (callback.data or "").split(":", 2)[2] if callback.data else ""
    if market not in _market_enabled:
        await callback.answer("Неизвестный маркет")
        return

    cur = _market_enabled[market]
    both_on = cur.get("buy", True) and cur.get("sell", True)
    set_market_enabled(market, not both_on)

    auto_seller = _state.get("auto_seller")
    if auto_seller:
        auto_seller.set_market_buy_enabled(market, not both_on)
        auto_seller.set_market_sell_enabled(market, not both_on)
    scanner = _state.get("scanner")
    if scanner and hasattr(scanner, "set_market_buy_enabled"):
        scanner.set_market_buy_enabled(market, not both_on)

    name_map = {"mrkt": "MRKT", "getgems": "Getgems", "fragment": "Fragment", "portal": "Portal"}
    name = name_map.get(market, market)
    icon = "🟢" if not both_on else "🔴"
    await callback.answer(f"{name}: {'ВКЛ' if not both_on else 'ВЫКЛ'}")
    if callback.message:
        await callback.message.edit_text(
            f"{icon} <b>{name}</b>: {'ВКЛ' if not both_on else 'ВЫКЛ'}",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("stg:mkt:"))
async def stg_market_buysell_toggle(callback: CallbackQuery) -> None:
    """Toggle buy or sell for a specific market."""
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Ошибка")
        return
    market = parts[2]
    action = parts[3]  # "buy" or "sell"

    if market not in _market_enabled or action not in ("buy", "sell"):
        await callback.answer("Неизвестный маркет/действие")
        return

    cur = _market_enabled[market][action]
    _market_enabled[market][action] = not cur
    new_val = not cur

    auto_seller = _state.get("auto_seller")
    scanner = _state.get("scanner")

    if action == "buy":
        if auto_seller and hasattr(auto_seller, "set_market_buy_enabled"):
            auto_seller.set_market_buy_enabled(market, new_val)
        if scanner and hasattr(scanner, "set_market_buy_enabled"):
            scanner.set_market_buy_enabled(market, new_val)
    else:
        if auto_seller and hasattr(auto_seller, "set_market_sell_enabled"):
            auto_seller.set_market_sell_enabled(market, new_val)

    name_map = {"mrkt": "MRKT", "getgems": "Getgems", "fragment": "Fragment", "portal": "Portal"}
    action_label = "покупка" if action == "buy" else "продажа"
    name = name_map.get(market, market)
    icon = "🟢" if new_val else "🔴"

    await callback.answer(f"{name} {action_label}: {'ВКЛ' if new_val else 'ВЫКЛ'}")
    if callback.message:
        await callback.message.edit_text(
            f"{icon} <b>{name} {action_label}</b>: {'ВКЛ' if new_val else 'ВЫКЛ'}",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("stg:sellmode:"))
async def stg_sellmode_toggle(callback: CallbackQuery) -> None:
    """Toggle sell price mode between floor and instant."""
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Ошибка")
        return
    market = parts[2]
    if market not in _sell_price_mode:
        await callback.answer("Неизвестный маркет")
        return

    cur = _sell_price_mode[market]
    new_mode = "floor" if cur == "instant" else "instant"
    _sell_price_mode[market] = new_mode

    name_map = {"mrkt": "MRKT", "portal": "Portal"}
    name = name_map.get(market, market)
    label = "мгновенная" if new_mode == "instant" else "floor"

    await callback.answer(f"{name}: {label}")
    if callback.message:
        await callback.message.edit_text(
            f"📊 <b>{name} цена продажи</b>: {label}",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "stg:pause")
async def stg_pause(callback: CallbackQuery) -> None:
    scanner = _state["scanner"]
    if scanner:
        scanner._paused = not scanner._paused
    mode = "⏸ Пауза" if (scanner and scanner._paused) else "▶️ Работает"
    await callback.answer(f"Сканер: {mode}")
    if callback.message:
        await callback.message.edit_text(
            f"🔄 Сканер: <b>{mode}</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "stg:auth")
async def stg_auth(callback: CallbackQuery) -> None:
    """Show auth status as inline response."""
    mrkt = _state.get("mrkt")
    gg = _state.get("gg")
    mds = _state.get("mds")
    portal = _state.get("portal_client")
    frag = mds._fragment if mds else None

    lines = ["🔑 <b>Статус доступов</b>\n"]

    mrkt_exp = bool(mrkt and mrkt._token_alert_sent)
    mrkt_ok = bool(mrkt and mrkt._auth_token)
    icon = "🔴" if mrkt_exp else ("🟢" if mrkt_ok else "❌")
    lines.append(f"{icon} MRKT → /token")

    gg_exp = bool(gg and gg._auth_alert_sent)
    gg_ok = bool(gg and gg._api_key)
    icon = "🔴" if gg_exp else ("🟢" if gg_ok else "❌")
    lines.append(f"{icon} Getgems → /getgems")

    frag_exp = bool(frag and frag._auth_alert_sent)
    frag_ok = bool(frag and frag.ready)
    icon = "🔴" if frag_exp else ("🟢" if frag_ok else "❌")
    lines.append(f"{icon} Fragment → /fragment")

    portal_exp = bool(portal and portal._auth_alert_sent)
    portal_ok = bool(portal and portal.authenticated)
    icon = "🔴" if portal_exp else ("🟢" if portal_ok else "❌")
    lines.append(f"{icon} Portal → /portal")

    wallet_ok = bool(gg and gg._wallet_address)
    icon = "🟢" if wallet_ok else "❌"
    lines.append(f"\n{icon} Кошелёк → /wallet")

    lines.append("\n<i>🔴 = истёк, 🟢 = ок, ❌ = не настроен</i>")

    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML"
        )


@router.callback_query(F.data == "stg:falling_price")
async def stg_falling_price(callback: CallbackQuery) -> None:
    """Toggle Getgems falling price listing mode."""
    from bot.config import settings as _cfg

    _cfg.falling_price_enabled = not _cfg.falling_price_enabled
    status = "ВКЛ 📉" if _cfg.falling_price_enabled else "ВЫКЛ"
    await callback.answer(f"Падающая цена: {status}")

    if _cfg.falling_price_enabled:
        interval_label = {
            300_000: "5м", 600_000: "10м", 1_800_000: "30м", 3_600_000: "1ч",
            10_800_000: "3ч", 21_600_000: "6ч", 43_200_000: "12ч",
            86_400_000: "1д", 172_800_000: "2д",
        }.get(_cfg.falling_price_interval_ms, "?")

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏱ Интервал", callback_data="stg:fp_interval"
                    ),
                    InlineKeyboardButton(
                        text="📉 Шаг снижения", callback_data="stg:fp_step"
                    ),
                ],
            ]
        )
        msg = (
            f"📉 <b>Падающая цена: ВКЛ</b>\n\n"
            f"Новые листинги на Getgems будут выставляться\n"
            f"с автоматическим снижением цены.\n\n"
            f"⏱ Интервал: каждые <b>{interval_label}</b>\n"
            f"📉 Шаг: <b>-{_cfg.falling_price_decrease_pct:.0f}%</b>\n"
            f"🔻 Минимум: цена покупки + комиссии"
        )
        if callback.message:
            await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=kb)
    else:
        if callback.message:
            await callback.message.edit_text(
                "📉 <b>Падающая цена: ВЫКЛ</b>\n\n"
                "Листинги будут по фиксированной цене.",
                parse_mode="HTML",
            )


@router.callback_query(F.data == "stg:fp_interval")
async def stg_fp_interval(callback: CallbackQuery) -> None:
    """Show interval options for falling price."""
    intervals = [
        (300_000, "5 мин"), (600_000, "10 мин"), (1_800_000, "30 мин"),
        (3_600_000, "1 час"), (10_800_000, "3 часа"), (21_600_000, "6 часов"),
        (43_200_000, "12 часов"), (86_400_000, "1 день"), (172_800_000, "2 дня"),
    ]
    buttons = []
    row: list[InlineKeyboardButton] = []
    for ms, label in intervals:
        row.append(InlineKeyboardButton(
            text=label, callback_data=f"stg:fp_interval_set:{ms}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if callback.message:
        await callback.message.edit_text(
            "⏱ <b>Интервал снижения цены:</b>\n"
            "Как часто цена будет автоматически снижаться?",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("stg:fp_interval_set:"))
async def stg_fp_interval_set(callback: CallbackQuery) -> None:
    """Set falling price interval."""
    from bot.config import settings as _cfg

    ms = int((callback.data or "").split(":")[-1])
    _cfg.falling_price_interval_ms = ms
    interval_label = {
        300_000: "5 мин", 600_000: "10 мин", 1_800_000: "30 мин",
        3_600_000: "1 час", 10_800_000: "3 часа", 21_600_000: "6 часов",
        43_200_000: "12 часов", 86_400_000: "1 день", 172_800_000: "2 дня",
    }.get(ms, "?")
    await callback.answer(f"Интервал: {interval_label}")
    if callback.message:
        await callback.message.edit_text(
            f"⏱ Интервал: <b>{interval_label}</b>\n"
            f"📉 Шаг: <b>-{_cfg.falling_price_decrease_pct:.0f}%</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "stg:fp_step")
async def stg_fp_step(callback: CallbackQuery) -> None:
    """Show step options for falling price decrease percentage."""
    steps = [1.0, 2.0, 3.0, 5.0, 7.0, 10.0]
    buttons = []
    row: list[InlineKeyboardButton] = []
    for pct in steps:
        row.append(InlineKeyboardButton(
            text=f"-{pct:.0f}%", callback_data=f"stg:fp_step_set:{pct}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if callback.message:
        await callback.message.edit_text(
            "📉 <b>Шаг снижения цены:</b>\n"
            "На сколько % снижать цену каждый интервал?",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("stg:fp_step_set:"))
async def stg_fp_step_set(callback: CallbackQuery) -> None:
    """Set falling price decrease percentage."""
    from bot.config import settings as _cfg

    pct = float((callback.data or "").split(":")[-1])
    _cfg.falling_price_decrease_pct = pct
    await callback.answer(f"Шаг: -{pct:.0f}%")
    if callback.message:
        interval_label = {
            300_000: "5 мин", 600_000: "10 мин", 1_800_000: "30 мин",
            3_600_000: "1 час", 10_800_000: "3 часа", 21_600_000: "6 часов",
            43_200_000: "12 часов", 86_400_000: "1 день", 172_800_000: "2 дня",
        }.get(_cfg.falling_price_interval_ms, "?")
        await callback.message.edit_text(
            f"⏱ Интервал: <b>{interval_label}</b>\n"
            f"📉 Шаг: <b>-{pct:.0f}%</b>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("stg:edit:"))
async def stg_edit(callback: CallbackQuery) -> None:
    key = callback.data.split(":", 2)[2] if callback.data else ""
    label = _SETTING_LABELS.get(key, key)
    options = _SETTING_OPTIONS.get(key, [])
    current = _runtime.get(key, 0)

    buttons = []
    row: list[InlineKeyboardButton] = []
    for val in options:
        mark = "✅ " if abs(val - current) < 0.01 else ""
        unit = "%" if "pct" in key else ("с" if "interval" in key else " TON")
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{val:.0f}{unit}",
                callback_data=f"stg:set:{key}:{val}",
            )
        )
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="stg:back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if callback.message:
        await callback.message.edit_text(
            f"⚙️ <b>{label}</b>\n\nТекущее: <b>{current}</b>\nВыбери новое значение:",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("stg:set:"))
async def stg_set(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    _waiting_setting.pop(uid, None)  # clear pending state

    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Ошибка")
        return
    key = parts[2]
    try:
        value = float(parts[3])
    except ValueError:
        await callback.answer("Ошибка значения")
        return

    _apply_setting(key, value)
    label = _SETTING_LABELS.get(key, key)

    await callback.answer(f"{label}: {value}")
    if callback.message:
        await callback.message.edit_text(
            f"✅ <b>{label}</b> = <b>{value:g}</b>\n\nНажми ⚙️ Настройки чтобы вернуться.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "stg:back")
async def stg_back(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.delete()
    # User can tap ⚙️ Настройки button again


# ── Retry buy after top-up ─────────────────────────────────────────────


@router.callback_query(F.data.startswith("retry_buy:"))
async def retry_buy(callback: CallbackQuery) -> None:
    """User clicked 'retry buy' after topping up wallet."""
    key = (callback.data or "").split(":", 1)[1] if callback.data else ""
    auto_seller = _state.get("auto_seller")
    if not auto_seller or key not in auto_seller._retry_queue:
        await callback.answer("Эта покупка уже недоступна")
        return

    info = auto_seller._retry_queue.pop(key)
    nft_addr = info["nft_addr"]
    version = info["version"]
    item_name = info["item_name"]
    item_price = info["item_price"]
    mrkt_floor = info["mrkt_floor"]

    gg = _state.get("gg")
    if not gg:
        await callback.answer("Getgems не подключён")
        return

    # Check balance again
    balance = await gg.get_wallet_balance()
    required = item_price + 100_000_000
    if balance < required:
        await callback.answer(f"Всё ещё мало: {balance / 1e9:.2f} TON")
        if callback.message:
            await callback.message.edit_text(
                f"⚠️ <b>Всё ещё недостаточно TON</b>\n\n"
                f"🎁 {item_name}\n"
                f"💎 Нужно: <b>{required / 1e9:.2f}</b> TON\n"
                f"💰 Баланс: <b>{balance / 1e9:.2f}</b> TON\n\n"
                f"Пополни ещё и нажми снова:",
                parse_mode="HTML",
                reply_markup=callback.message.reply_markup,
            )
        # Put back in queue
        auto_seller._retry_queue[key] = info
        return

    await callback.answer("Покупаю...")
    if callback.message:
        await callback.message.edit_text(
            f"⏳ <b>Покупаю на Getgems...</b>\n\n"
            f"🎁 {item_name}\n"
            f"💎 Цена: <b>{item_price / 1e9:.2f}</b> TON",
            parse_mode="HTML",
        )

    success = await gg.buy_gift(nft_addr, version)
    if success:
        if callback.message:
            await callback.message.edit_text(
                f"✅ <b>Куплено на Getgems!</b>\n\n"
                f"🎁 {item_name}\n"
                f"💎 Цена: <b>{item_price / 1e9:.2f}</b> TON\n"
                f"🟦 MRKT floor: <b>{mrkt_floor / 1e9:.2f}</b> TON\n\n"
                f"Теперь забери подарок из Getgems и переведи на MRKT",
                parse_mode="HTML",
            )
    else:
        if callback.message:
            await callback.message.edit_text(
                f"❌ <b>Ошибка покупки</b>\n\n"
                f"🎁 {item_name}\n"
                f"Возможно NFT уже продан. Бот найдёт новые возможности.",
                parse_mode="HTML",
            )


@router.message(Command("autosell"))
async def cmd_autosell(message: types.Message) -> None:
    auto_seller = _state.get("auto_seller")
    if auto_seller:
        auto_seller.enabled = not auto_seller.enabled
        mode = "ВКЛ 🟢" if auto_seller.enabled else "ВЫКЛ 🔴"
        await message.answer(
            f"🤖 Автопродажа: <b>{mode}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer("Автопродажа не настроена", reply_markup=MAIN_KB)


@router.message(Command("log"))
async def cmd_log(message: types.Message) -> None:
    """Show recent scanner and auto-seller activity."""
    import datetime as dt

    lines: list[str] = ["📋 <b>Последние действия</b>\n"]

    scanner = _state["scanner"]
    if scanner:
        s = scanner.get_status()
        activity = s.get("activity", [])
        for a in activity[-10:]:
            ts = dt.datetime.fromtimestamp(a["time"]).strftime("%H:%M:%S")
            event = a.get("event", "?")
            coll = a.get("collection", "")
            if event == "scan":
                mrkt_f = a.get("mrkt_floor", 0)
                gg_f = a.get("gg_floor", 0)
                lines.append(
                    f"🔍 {ts} {coll}\n"
                    f"   MRKT: {_ton(mrkt_f)} | GG: {_ton(gg_f)} | {a.get('listings', 0)} шт."
                )
            elif event == "opportunity":
                lines.append(f"🎯 {ts} {coll} — {a.get('type', '?')}")
            elif event == "error":
                lines.append(f"⚠️ {ts} {coll} — {a.get('msg', '?')}")

    auto_seller = _state.get("auto_seller")
    if auto_seller:
        ast = auto_seller.get_status()
        for a in ast.get("log", [])[-5:]:
            ts = dt.datetime.fromtimestamp(a["time"]).strftime("%H:%M:%S")
            event = a.get("event", "?")
            gift_name = a.get("gift", "")
            if event == "listed":
                lines.append(
                    f"🏷 {ts} {gift_name} → {a.get('market', '?')} {a.get('price', 0):.2f} TON"
                )
            elif event == "repriced":
                lines.append(
                    f"💲 {ts} {gift_name} {a.get('old', 0):.2f} → {a.get('new', 0):.2f} TON"
                )
            elif event == "recommend_gg":
                lines.append(f"💎 {ts} {gift_name} — лучше на Getgems {a.get('price', 0):.2f} TON")

    if len(lines) == 1:
        lines.append("Пока нет действий")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KB)


@router.message(Command("pause"))
async def cmd_pause(message: types.Message) -> None:
    scanner = _state["scanner"]
    if scanner:
        scanner._paused = not scanner._paused
        if scanner._paused:
            await message.answer("⏸ Сканер на паузе", reply_markup=MAIN_KB)
        else:
            await message.answer("▶️ Сканер возобновлён", reply_markup=MAIN_KB)
    else:
        await message.answer("Сканер не запущен", reply_markup=MAIN_KB)


# ── Token Update ───────────────────────────────────────────────────────


@router.message(Command("token"))
async def cmd_token(message: types.Message) -> None:
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "🔑 <b>Обновление MRKT токена</b>\n\n"
            "Использование:\n<code>/token новый_токен</code>\n\n"
            "Токен обновится на лету без перезапуска.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    new_token = parts[1].strip()
    mrkt = _state["mrkt"]
    if not mrkt:
        await message.answer("❌ MRKT клиент не запущен", reply_markup=MAIN_KB)
        return

    mrkt.update_token(new_token)

    # Delete the message with token for security
    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        "✅ <b>MRKT токен обновлён!</b>\n\nСообщение с токеном удалено из чата.",
        parse_mode="HTML",
        reply_markup=MAIN_KB,
    )


# ── Portal Token Update ────────────────────────────────────────────────


@router.message(Command("portal"))
async def cmd_portal_token(message: types.Message) -> None:
    """Update Portal TMA init data at runtime."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        portal = _state.get("portal_client")
        status = "✅ Активен" if (portal and portal.authenticated) else "❌ Нет токена"
        await message.answer(
            f"🟣 <b>Portal Market авторизация</b>\n\n"
            f"Статус: {status}\n\n"
            f"Отправь TMA init data (Authorization header без 'tma '):\n"
            f"<code>/portal user=...&amp;hash=...</code>\n\n"
            f"Получить: откройте @portals_market_bot → "
            f"перехватите запрос → скопируйте Authorization header.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    new_data = parts[1].strip()
    # Strip "tma " prefix if user included it
    if new_data.lower().startswith("tma "):
        new_data = new_data[4:]

    portal = _state.get("portal_client")
    if not portal:
        await message.answer("❌ Portal клиент не запущен", reply_markup=MAIN_KB)
        return

    portal.update_auth(new_data)

    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        "✅ <b>Portal токен обновлён!</b>\n\n"
        "Сообщение с токеном удалено из чата.",
        parse_mode="HTML",
        reply_markup=MAIN_KB,
    )


# ── Fragment Cookies Update ─────────────────────────────────────────────


@router.message(Command("fragment"))
async def cmd_fragment_cookies(message: types.Message) -> None:
    """Update Fragment cookies at runtime."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        mds = _state.get("mds")
        frag = mds._fragment if mds else None
        status = "✅ Активен" if (frag and frag.ready) else "❌ Нет cookies"
        await message.answer(
            f"💜 <b>Fragment авторизация</b>\n\n"
            f"Статус: {status}\n\n"
            f"Отправь cookies в формате JSON:\n"
            f'<code>/fragment {{"stel_token":"...", '
            f'"stel_ton_token":"..."}}</code>\n\n'
            f"Получить: откройте fragment.com → DevTools → "
            f"Application → Cookies.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    import json as _json

    cookie_str = parts[1].strip()
    try:
        cookies = _json.loads(cookie_str)
    except _json.JSONDecodeError:
        await message.answer(
            "❌ Невалидный JSON. Формат:\n"
            '<code>{"stel_token":"...", "stel_ton_token":"..."}</code>',
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    mds = _state.get("mds")
    frag = mds._fragment if mds else None
    if not frag:
        await message.answer("❌ Fragment клиент не запущен", reply_markup=MAIN_KB)
        return

    frag.update_cookies(cookies)
    # Trigger reinitialize
    ok = await frag.initialize()

    try:
        await message.delete()
    except Exception:
        pass

    if ok:
        await message.answer(
            "✅ <b>Fragment cookies обновлены!</b>\n\n"
            "Клиент переподключён. Сообщение удалено.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer(
            "⚠️ Cookies обновлены, но инициализация Fragment не удалась.\n"
            "Проверьте cookies и попробуйте снова.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )


# ── Getgems API Key Update ──────────────────────────────────────────────


@router.message(Command("getgems"))
async def cmd_getgems_key(message: types.Message) -> None:
    """Update Getgems API key at runtime."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        gg = _state.get("gg")
        has_key = bool(gg and gg._api_key)
        status = "✅ Активен" if has_key else "❌ Нет ключа"
        await message.answer(
            f"💎 <b>Getgems API ключ</b>\n\n"
            f"Статус: {status}\n\n"
            f"<code>/getgems новый_api_key</code>",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    new_key = parts[1].strip()
    gg = _state.get("gg")
    if not gg:
        await message.answer("❌ Getgems клиент не запущен", reply_markup=MAIN_KB)
        return

    gg.update_api_key(new_key)

    try:
        await message.delete()
    except Exception:
        pass

    await message.answer(
        "✅ <b>Getgems API key обновлён!</b>\n\n"
        "Сообщение удалено из чата.",
        parse_mode="HTML",
        reply_markup=MAIN_KB,
    )


# ── Credentials Status ──────────────────────────────────────────────────


@router.message(Command("auth"))
async def cmd_auth_status(message: types.Message) -> None:
    """Show auth status for all marketplaces."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    mrkt = _state.get("mrkt")
    gg = _state.get("gg")
    mds = _state.get("mds")
    portal = _state.get("portal_client")
    frag = mds._fragment if mds else None

    lines = ["🔑 <b>Статус авторизации</b>\n"]

    # MRKT
    mrkt_ok = bool(mrkt and mrkt._auth_token)
    mrkt_exp = bool(mrkt and mrkt._token_alert_sent)
    icon = "🔴 ИСТЁК" if mrkt_exp else ("🟢" if mrkt_ok else "❌")
    lines.append(f"{icon} <b>MRKT</b>: /token")

    # Getgems
    gg_ok = bool(gg and gg._api_key)
    gg_exp = bool(gg and gg._auth_alert_sent)
    icon = "🔴 ИСТЁК" if gg_exp else ("🟢" if gg_ok else "❌")
    lines.append(f"{icon} <b>Getgems</b>: /getgems")

    # Fragment
    frag_ok = bool(frag and frag.ready)
    frag_exp = bool(frag and frag._auth_alert_sent)
    icon = "🔴 ИСТЁК" if frag_exp else ("🟢" if frag_ok else "❌")
    lines.append(f"{icon} <b>Fragment</b>: /fragment")

    # Portal
    portal_ok = bool(portal and portal.authenticated)
    portal_exp = bool(portal and portal._auth_alert_sent)
    icon = "🔴 ИСТЁК" if portal_exp else ("🟢" if portal_ok else "❌")
    lines.append(f"{icon} <b>Portal</b>: /portal")

    # Wallet
    wallet_ok = bool(gg and gg._wallet_address)
    icon = "🟢" if wallet_ok else "❌"
    addr = f" ({gg._wallet_address[:8]}...)" if wallet_ok else ""
    lines.append(f"\n{icon} <b>Кошелёк</b>{addr}: /wallet")

    lines.append(
        "\n<i>Для обновления нажмите на команду нужного маркета.</i>"
    )

    await message.answer(
        "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KB
    )


# ── Deals diagnostic ────────────────────────────────────────────────────


@router.message(Command("deals"))
async def cmd_deals(message: types.Message) -> None:
    """Show active deals in DB (exposure breakdown)."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from bot.models.database import Deal, async_session
    from bot.models.types import DealState, nanoton_to_ton

    active_states = [
        DealState.BOUGHT.value,
        DealState.WITHDRAWING.value,
        DealState.WITHDRAWN.value,
        DealState.AWAITING_TRANSFER.value,
        DealState.TRANSFER_CONFIRMED.value,
        DealState.MONITORING_GETGEMS.value,
        DealState.LISTING.value,
        DealState.LISTED.value,
        DealState.RELISTING.value,
    ]

    async with async_session() as s:
        result = await s.execute(
            select(
                Deal.state,
                Deal.is_shadow,
                sa_func.count(),
                sa_func.sum(Deal.buy_price),
            )
            .where(Deal.state.in_(active_states))
            .group_by(Deal.state, Deal.is_shadow)
        )
        rows = result.all()

        # Top deals by exposure
        top_result = await s.execute(
            select(
                Deal.id,
                Deal.collection_name,
                Deal.buy_price,
                Deal.state,
                Deal.is_shadow,
                Deal.detected_at,
            )
            .where(
                Deal.state.in_(active_states),
                Deal.is_shadow.is_(False),
            )
            .order_by(Deal.buy_price.desc())
            .limit(15)
        )
        top_deals = top_result.all()

    lines = ["📋 <b>Активные сделки в БД</b>\n"]
    total_real = 0
    total_shadow = 0

    for state, is_shadow, count, total_price in rows:
        ton = nanoton_to_ton(total_price or 0)
        label = "👻" if is_shadow else "💰"
        lines.append(f"  {label} {state}: {count} шт, {ton:.1f} TON")
        if is_shadow:
            total_shadow += (total_price or 0)
        else:
            total_real += (total_price or 0)

    lines.append(
        f"\n<b>Итого реальных: {nanoton_to_ton(total_real):.1f} TON</b>"
    )
    lines.append(f"👻 Shadow: {nanoton_to_ton(total_shadow):.1f} TON")

    if top_deals:
        lines.append("\n<b>Топ реальных сделок:</b>")
        for did, coll, price, state, shadow, detected in top_deals:
            ton = nanoton_to_ton(price)
            age = ""
            if detected:
                try:
                    from datetime import datetime
                    from datetime import timezone as _tz

                    if detected.tzinfo is None:
                        detected = detected.replace(tzinfo=_tz.utc)
                    hours = (
                        datetime.now(_tz.utc) - detected
                    ).total_seconds() / 3600
                    age = f" ({hours:.0f}ч)"
                except Exception:
                    pass
            lines.append(
                f"  #{did} {coll}: {ton:.1f} TON [{state}]{age}"
            )

    lines.append(
        "\n<i>/reset_deals — сбросить все фантомные сделки</i>"
    )

    await message.answer(
        "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KB
    )


@router.message(Command("reset_deals"))
async def cmd_reset_deals(message: types.Message) -> None:
    """Cancel all active non-shadow deals (phantom cleanup)."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    from sqlalchemy import update

    from bot.models.database import Deal, async_session
    from bot.models.types import DealState

    active_states = [
        DealState.BOUGHT.value,
        DealState.WITHDRAWING.value,
        DealState.WITHDRAWN.value,
        DealState.AWAITING_TRANSFER.value,
        DealState.TRANSFER_CONFIRMED.value,
        DealState.MONITORING_GETGEMS.value,
        DealState.LISTING.value,
        DealState.LISTED.value,
        DealState.RELISTING.value,
    ]

    async with async_session() as s:
        result = await s.execute(
            update(Deal)
            .where(Deal.state.in_(active_states))
            .values(state=DealState.CANCELLED.value)
            .execution_options(synchronize_session=False)
        )
        await s.commit()
        count = result.rowcount

    await message.answer(
        f"🗑 <b>Сброшено {count} сделок</b>\n\n"
        f"Все активные сделки переведены в статус CANCELLED.\n"
        f"Risk engine теперь позволит новые покупки.",
        parse_mode="HTML",
        reply_markup=MAIN_KB,
    )


# ── Wallet (Getgems auto-sell) ──────────────────────────────────────────


@router.message(Command("wallet"))
async def cmd_wallet(message: types.Message) -> None:
    """Set TON wallet mnemonic for Getgems auto-listing."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        gg = _state.get("gg")
        wallet_status = "❌ Не настроен"
        if gg and gg._wallet_address:
            addr = gg._wallet_address
            wallet_status = f"✅ {addr[:8]}...{addr[-6:]}"

        await message.answer(
            f"💎 <b>Кошелёк для Getgems</b>\n\n"
            f"Статус: {wallet_status}\n\n"
            f"Для автопродажи на Getgems нужна мнемоника кошелька (24 слова).\n\n"
            f"<code>/wallet слово1 слово2 ... слово24</code>\n\n"
            f"⚠️ Мнемоника будет удалена из чата и хранится только в памяти.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    mnemonic = parts[1].strip()

    # Delete message with mnemonic immediately
    try:
        await message.delete()
    except Exception:
        pass

    gg = _state.get("gg")
    if not gg:
        await message.answer("❌ Getgems клиент не запущен", reply_markup=MAIN_KB)
        return

    success = await gg.authenticate_rest(mnemonic)
    if success:
        await message.answer(
            f"✅ <b>Кошелёк подключён!</b>\n\n"
            f"Адрес: <code>{gg._wallet_address}</code>\n"
            f"Автопродажа на Getgems теперь доступна.\n"
            f"Мнемоника удалена из чата.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer(
            "❌ Ошибка подключения кошелька.\nПроверьте мнемонику (24 слова через пробел).",
            reply_markup=MAIN_KB,
        )


# ── Track gift on Getgems ──────────────────────────────────────────────


@router.message(Command("track"))
async def cmd_track(message: types.Message) -> None:
    """Manually add a gift to Getgems monitoring queue.

    Usage: /track Lol Pop 278287 2.45
    """
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    text = (message.text or "").strip()
    parts = text.split()
    # /track Name Name 123456 2.45
    if len(parts) < 4:
        await message.answer(
            "📡 <b>Отслеживание подарка на Getgems</b>\n\n"
            "Использование:\n"
            "<code>/track Название Номер Цена_TON</code>\n\n"
            "Пример:\n"
            "<code>/track Lol Pop 278287 2.45</code>\n\n"
            "Бот будет искать подарок на Getgems и автоматически выставит на продажу.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    try:
        price_ton = float(parts[-1])
        number = parts[-2]
        name = " ".join(parts[1:-2])
    except (ValueError, IndexError):
        await message.answer("❌ Формат: /track Название Номер Цена_TON", reply_markup=MAIN_KB)
        return

    price_nano = int(price_ton * 1_000_000_000)
    auto_seller = _state.get("auto_seller")
    if auto_seller:
        auto_seller.add_pending_getgems(name, price_nano, number)
        await message.answer(
            f"📡 <b>Отслеживаю {name} #{number}</b>\n\n"
            f"Цена: {price_ton:.2f} TON\n"
            f"Бот проверяет Getgems каждые 2 минуты.\n"
            f"Как только подарок появится — автоматически выставит на продажу.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer("❌ AutoSeller не запущен", reply_markup=MAIN_KB)


# ── Transfer Confirmation ──────────────────────────────────────────────


@router.callback_query(lambda c: c.data and c.data.startswith("transfer_confirm:"))
async def on_transfer_confirm(callback: CallbackQuery) -> None:
    if not callback.data:
        return

    deal_id = int(callback.data.split(":")[1])
    deal = await state_machine.get_deal(deal_id)

    if not deal:
        await callback.answer("Сделка не найдена")
        return

    if deal.state != DealState.AWAITING_TRANSFER.value:
        await callback.answer(f"Неожиданное состояние: {deal.state}")
        return

    success = await state_machine.transition(
        deal_id,
        DealState.AWAITING_TRANSFER,
        DealState.TRANSFER_CONFIRMED,
        reason="user_confirmed",
    )

    if success:
        await callback.answer("Подтверждено!")
        if callback.message:
            await callback.message.answer(
                f"✅ Перевод подтверждён для сделки #{deal_id}\nМониторю появление на Getgems...",
            )
    else:
        await callback.answer("Ошибка при подтверждении")


# ── Debug (audit) ──────────────────────────────────────────────────────


@router.message(Command("debug"))
async def cmd_debug(message: types.Message) -> None:
    """Dump raw gift data for first few inventory items."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return

    import json as _json

    mrkt = _state["mrkt"]
    if not mrkt:
        await message.answer("MRKT клиент не запущен", reply_markup=MAIN_KB)
        return

    gifts = await mrkt.get_my_gifts(owner_tg_id=_state["admin_chat_id"], count=5)
    if not gifts:
        await message.answer("Инвентарь пуст или ошибка API", reply_markup=MAIN_KB)
        return

    lines: list[str] = [f"🔍 <b>Raw API (первые {len(gifts)} подарков)</b>\n"]
    for g in gifts[:3]:
        # Show all keys and sale-related fields
        name = g.get("collectionName", "?")
        number = g.get("number", "?")
        lines.append(f"<b>{name} #{number}</b>")
        for key in sorted(g.keys()):
            val = g[key]
            if isinstance(val, (dict, list)):
                val = _json.dumps(val, ensure_ascii=False)[:100]
            lines.append(f"  <code>{key}</code>: {val}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n... (обрезано)"

    await message.answer(text, parse_mode="HTML", reply_markup=MAIN_KB)


# ── Free-form setting commands ─────────────────────────────────────────

_TEXT_SETTINGS: dict[str, tuple[str, str]] = {
    # keyword -> (runtime_key, label)
    "рои": ("min_roi_pct", "Мин. ROI покупка (%)"),
    "roi": ("min_roi_pct", "Мин. ROI покупка (%)"),
    "кросс": ("cross_roi_pct", "Мин. ROI кросс-маркет (%)"),
    "cross": ("cross_roi_pct", "Мин. ROI кросс-маркет (%)"),
    "ордер": ("order_roi_pct", "Мин. ROI ордер-арб (%)"),
    "order": ("order_roi_pct", "Мин. ROI ордер-арб (%)"),
    "скидка": ("deep_roi_pct", "Мин. ROI скидка (%)"),
    "deep": ("deep_roi_pct", "Мин. ROI скидка (%)"),
    "интервал": ("scan_interval", "Интервал скана (сек)"),
    "interval": ("scan_interval", "Интервал скана (сек)"),
    "макс": ("max_buy_ton", "Макс. покупка (TON)"),
    "max": ("max_buy_ton", "Макс. покупка (TON)"),
    "лимит": ("max_buy_ton", "Макс. покупка (TON)"),
    "экспозиция": ("max_exposure_ton", "Макс. экспозиция (TON)"),
    "exposure": ("max_exposure_ton", "Макс. экспозиция (TON)"),
    "автопродажа": ("autosell_interval", "Интервал автопродажи (сек)"),
}


@router.callback_query(F.data == "stg:cancel")
async def stg_cancel(callback: CallbackQuery) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    _waiting_setting.pop(uid, None)
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.delete()


@router.message(F.text == "📦 Подарки")
async def btn_gifts(message: types.Message) -> None:
    """Show gifts on account with transfer targets."""
    gt = _state.get("gift_transfer")
    if not gt or not gt.ready:
        await message.answer(
            "📦 <b>Подарки</b>\n\n❌ Telegram сессия не подключена.\nПередача подарков недоступна.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    gifts = await gt.get_my_gifts(limit=50)
    if not gifts:
        await message.answer(
            "📦 <b>Подарки</b>\n\nНет подарков на аккаунте.",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    targets = gt._targets  # market -> username
    mds = _state.get("mds")

    lines = [f"📦 <b>Подарки на аккаунте</b> ({len(gifts)} шт.)\n"]
    gift_buttons: list[list[InlineKeyboardButton]] = []

    for g in gifts:
        name = g.get("title") or g.get("slug") or f"Gift #{g.get('gift_id', '?')}"
        is_unique = g.get("is_unique", False)
        hold_sec = g.get("hold_seconds", 0)
        transferable = g.get("transferable", False)
        gift_id = str(g.get("gift_id", ""))

        # Status
        if not is_unique:
            status = "⚪ обычный"
        elif transferable:
            status = "🟢 готов"
        elif hold_sec > 0:
            days = hold_sec // 86400
            hours = (hold_sec % 86400) // 3600
            if days > 0:
                status = f"🔒 {days}д {hours}ч"
            else:
                status = f"🔒 {hours}ч"
        else:
            status = "🟡 ожидание"

        # Get best sell market from cached floors
        gift_coll = name.split("#")[0].strip() if "#" in name else name
        best_target = ""
        best_price_ton = 0.0
        if mds and is_unique:
            mrkt_fl = 0
            mrkt_bulk = mds.get_all_mrkt_floors()
            for k, v in mrkt_bulk.items():
                if k.lower() == gift_coll.lower() or gift_coll.lower() in k.lower():
                    mrkt_fl = v
                    break
            gg_fl = mds.get_cached_gg_floor(gift_coll)
            if gg_fl > mrkt_fl and gg_fl > 0:
                best_target = targets.get("getgems", "gemsrelayer")
                best_price_ton = gg_fl / 1e9
            elif mrkt_fl > 0:
                best_target = targets.get("mrkt", "mrktbank")
                best_price_ton = mrkt_fl / 1e9

        # Build line
        icon = "💎" if is_unique else "⭐"
        line = f"{icon} <b>{name}</b> — {status}"
        if best_target and best_price_ton > 0:
            line += f"\n  → @{best_target} ({best_price_ton:.1f} TON)"

        lines.append(line)

        # Add inline button for unique gifts
        if is_unique and gift_id:
            btn_label = f"{'🟢' if transferable else '🔒'} {name}"
            if best_target:
                btn_label += f" → @{best_target}"
            # Truncate to fit 64-byte limit
            if len(f"tg_gift:{gift_id}") <= 64:
                gift_buttons.append(
                    [
                        InlineKeyboardButton(
                            text=btn_label[:60],
                            callback_data=f"tg_gift:{gift_id}",
                        )
                    ]
                )

    # Summary
    unique_count = sum(1 for g in gifts if g.get("is_unique"))
    transferable_count = sum(1 for g in gifts if g.get("transferable"))
    on_hold = sum(1 for g in gifts if g.get("is_unique") and g.get("hold_seconds", 0) > 0)

    lines.append(
        f"\n📊 Уникальных: {unique_count} | Готовы: {transferable_count} | На холде: {on_hold}"
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (обрезано)"

    # Build keyboard with gift buttons + refresh
    gift_buttons.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="tg_gifts_refresh")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=gift_buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("tg_gift:"))
async def cb_tg_gift_detail(callback: CallbackQuery) -> None:
    """Show detail for a Telegram gift with price comparison."""
    gift_id = callback.data.split(":")[1]  # type: ignore[union-attr]
    gt = _state.get("gift_transfer")
    mds = _state.get("mds")

    if not gt or not gt.ready:
        await callback.answer("Сессия не подключена")
        return

    # Find the gift in current list
    all_gifts = await gt.get_my_gifts(limit=50)
    gift = next((g for g in all_gifts if str(g.get("gift_id", "")) == gift_id), None)

    if not gift:
        await callback.answer("Подарок не найден")
        return

    name = gift.get("title") or gift.get("slug") or f"Gift #{gift_id}"
    gift_coll = name.split("#")[0].strip() if "#" in name else name
    hold_sec = gift.get("hold_seconds", 0)
    transferable = gift.get("transferable", False)

    # Get prices from all markets
    mrkt_fl = 0
    gg_fl = 0
    frag_fl = 0
    portal_fl = 0
    if mds:
        mrkt_bulk = mds.get_all_mrkt_floors()
        for k, v in mrkt_bulk.items():
            if k.lower() == gift_coll.lower() or gift_coll.lower() in k.lower():
                mrkt_fl = v
                break
        gg_fl = mds.get_cached_gg_floor(gift_coll)
        frag_fl = mds.get_cached_fragment_floor(gift_coll)
        portal_fl = mds.get_cached_portal_floor(gift_coll)

    text = f"🎁 <b>{name}</b>\n\n"

    if transferable:
        text += "🟢 <b>Готов к передаче</b>\n\n"
    elif hold_sec > 0:
        days = hold_sec // 86400
        hours = (hold_sec % 86400) // 3600
        text += f"🔒 <b>Холд:</b> {days}д {hours}ч\n\n"

    text += "<b>Цены на площадках:</b>\n"
    if mrkt_fl > 0:
        text += f"🟦 MRKT: {mrkt_fl / 1e9:.2f} TON\n"
    if gg_fl > 0:
        text += f"💎 Getgems: {gg_fl / 1e9:.2f} TON\n"
    if frag_fl > 0:
        text += f"💜 Fragment: {frag_fl / 1e9:.2f} TON\n"
    if portal_fl > 0:
        text += f"🟣 Portal: {portal_fl / 1e9:.2f} TON\n"

    # Best market
    targets = gt._targets
    markets = [(mrkt_fl, "mrkt"), (gg_fl, "getgems"), (frag_fl, "fragment"), (portal_fl, "portal")]
    markets = [(p, m) for p, m in markets if p > 0]
    if markets:
        markets.sort(key=lambda x: x[0], reverse=True)
        best_price, best_m = markets[0]
        labels = {"mrkt": "MRKT", "getgems": "Getgems", "fragment": "Fragment", "portal": "Portal"}
        target = targets.get(best_m, "")
        text += f"\n⚡ <b>Лучше на {labels[best_m]}</b>"
        if target and best_m != "fragment":
            text += f" → @{target}"
        text += f" ({best_price / 1e9:.2f} TON)"

    buttons: list[list[InlineKeyboardButton]] = []
    if transferable:
        gg_target = targets.get("getgems", "gemsrelayer")
        mrkt_target = targets.get("mrkt", "mrktbank")
        if gg_fl > 0:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"💎 → @{gg_target} ({gg_fl / 1e9:.1f} TON)",
                        callback_data="noop",
                    )
                ]
            )
        if mrkt_fl > 0:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🟦 → @{mrkt_target} ({mrkt_fl / 1e9:.1f} TON)",
                        callback_data="noop",
                    )
                ]
            )
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tg_gifts_refresh")])

    if callback.message:
        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data == "tg_gifts_refresh")
async def cb_tg_gifts_refresh(callback: CallbackQuery) -> None:
    """Refresh the gifts list."""
    if callback.message:
        # Re-trigger the gifts view by simulating the command
        await btn_gifts(callback.message)  # type: ignore[arg-type]
    await callback.answer("Обновлено")


@router.message(F.text)
async def on_text_setting(message: types.Message) -> None:
    """Handle numbered settings menu + free-form text commands."""
    if not message.from_user or message.from_user.id != _state["admin_chat_id"]:
        return
    uid = message.from_user.id
    text = (message.text or "").strip()

    # ── Step 2: waiting for value after user selected a setting number ──
    pending_key = _waiting_setting.get(uid)
    if pending_key:
        _waiting_setting.pop(uid, None)
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            await message.answer(
                "❌ Введи число. Попробуй ещё раз.",
                reply_markup=MAIN_KB,
            )
            return
        _apply_setting(pending_key, value)
        label = _SETTING_LABELS.get(pending_key, pending_key)
        await message.answer(
            f"✅ <b>{label}</b> = <b>{value:g}</b>",
            parse_mode="HTML",
            reply_markup=MAIN_KB,
        )
        return

    # ── Step 1: user typed a number to select a setting ──
    text_lower = text.lower()
    try:
        num = int(text_lower)
        is_number = True
    except ValueError:
        is_number = False

    if is_number:
        if num == 0:
            scanner = _state.get("scanner")
            if scanner:
                scanner.shadow_mode = not scanner.shadow_mode
                auto_seller = _state.get("auto_seller")
                if auto_seller:
                    auto_seller._shadow_mode = scanner.shadow_mode
                execution = _state.get("execution")
                if execution:
                    execution.shadow_mode = scanner.shadow_mode
                mode = "ВКЛ 👻" if scanner.shadow_mode else "ВЫКЛ ⚠️"
                await message.answer(
                    f"👻 Теневой режим: <b>{mode}</b>",
                    parse_mode="HTML",
                    reply_markup=MAIN_KB,
                )
            return
        if 1 <= num <= len(_SETTING_KEYS):
            key = _SETTING_KEYS[num - 1]
            label = _SETTING_LABELS.get(key, key)
            current = _runtime[key]
            _waiting_setting[uid] = key

            options = _SETTING_OPTIONS.get(key, [])
            buttons: list[list[InlineKeyboardButton]] = []
            row: list[InlineKeyboardButton] = []
            for val in options:
                mark = "✅ " if abs(val - current) < 0.01 else ""
                unit = "%" if "pct" in key else ("с" if "interval" in key else " TON")
                row.append(
                    InlineKeyboardButton(
                        text=f"{mark}{val:g}{unit}",
                        callback_data=f"stg:set:{key}:{val}",
                    )
                )
                if len(row) == 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="stg:cancel")])

            kb = InlineKeyboardMarkup(inline_keyboard=buttons)
            await message.answer(
                f"⚙️ <b>{label}</b>\n\n"
                f"Текущее: <b>{current:g}</b>\n\n"
                f"Напиши число в чат или выбери кнопку:",
                parse_mode="HTML",
                reply_markup=kb,
            )
            return

    # ── Free-form keyword commands (e.g. "рои 3", "макс 50") ──
    parts = text_lower.split()
    if len(parts) == 2:
        keyword, raw_value = parts[0], parts[1]
        setting = _TEXT_SETTINGS.get(keyword)
        if setting:
            try:
                value = float(raw_value.replace(",", "."))
            except ValueError:
                return
            runtime_key, label = setting
            _apply_setting(runtime_key, value)
            await message.answer(
                f"✅ <b>{label}</b> = <b>{value:g}</b>",
                parse_mode="HTML",
                reply_markup=MAIN_KB,
            )
            return


# ── Bot Factory ────────────────────────────────────────────────────────


def create_bot(token: str) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
