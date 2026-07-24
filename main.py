"""Main entrypoint — wires all components and starts the system.

Startup sequence:
  1. Init database (create tables)
  2. Recover active deals from last run
  3. Init all services (API clients + trading modules)
  4. Load collections from MRKT
  5. Start scanner (shadow mode by default)
  6. Start Telegram bot
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from bot.config import settings
from bot.core import audit, state_machine
from bot.core.circuit_breaker import CircuitBreaker
from bot.core.locks import create_lock_manager
from bot.data.market_data_service import MarketDataService
from bot.data.wash_trade_detector import WashTradeDetector
from bot.execution.execution_engine import ExecutionEngine
from bot.execution.portfolio_manager import PortfolioManager
from bot.execution.risk_engine import RiskEngine
from bot.fragment_client import FragmentClient
from bot.getgems_client import GetgemsClient
from bot.intelligence.liquidation_model import LiquidationModel
from bot.intelligence.market_quality import MarketQualityFilter
from bot.intelligence.pricing_engine import PricingEngine
from bot.interface import telegram_bot
from bot.interface.notification import NotificationService
from bot.models.database import init_db
from bot.models.types import Market, MarketSnapshot, nanoton_to_ton
from bot.mrkt_client import MRKTClient
from bot.portal_client import PortalClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Starting Cross-Market Arbitrage System")

    # ── 1. Database ────────────────────────────────────────────────────
    reset_db = os.environ.get("RESET_DB", "").lower() == "true"
    if reset_db:
        logger.warning("RESET_DB=true — dropping and recreating all tables!")
    await init_db(drop_existing=reset_db)
    logger.info("Database initialized")

    # ── 2. Recovery ────────────────────────────────────────────────────
    recovery = await state_machine.recover_on_startup()
    await audit.log_system("startup", extra={"recovery": recovery})

    # ── 3. API clients ─────────────────────────────────────────────────
    mrkt = MRKTClient()
    gg = GetgemsClient(
        api_key=settings.getgems_api_key,
        cdp_url=settings.getgems_cdp_url,
        graphql_auth_token=settings.getgems_auth_token,
    )

    # Fragment client
    import json as _json

    fragment = FragmentClient(
        seed=os.environ.get("WALLET_MNEMONIC", ""),
        api_key=os.environ.get("FRAGMENT_API_KEY", settings.fragment_api_key),
        cookies=_json.loads(os.environ.get("FRAGMENT_COOKIES", "{}") or "{}")
        if os.environ.get("FRAGMENT_COOKIES")
        else None,
    )
    if fragment._seed and fragment._api_key and fragment._cookies:
        try:
            frag_ok = await fragment.initialize()
            if frag_ok:
                logger.info("Fragment client initialized")
            else:
                logger.warning("Fragment init failed — Fragment arbitrage disabled")
        except Exception as e:
            logger.warning("Fragment init error: %s", e)
    else:
        logger.info("No Fragment credentials — Fragment arbitrage disabled")

    # Portal Market client
    portal_tma = os.environ.get("PORTAL_TMA_INIT_DATA", settings.portal_tma_init_data)
    portal = PortalClient(tma_init_data=portal_tma)
    if portal.ready:
        logger.info("Portal Market client initialized (authenticated)")
    else:
        logger.info("No Portal TMA init data — Portal scanning only (no auth)")

    # ── 3b. Getgems wallet auth (for auto-listing) ─────────────────────
    wallet_mnemonic = os.environ.get("WALLET_MNEMONIC", "")
    if wallet_mnemonic:
        try:
            auth_ok = await gg.authenticate_rest(wallet_mnemonic)
            if auth_ok:
                logger.info("Getgems wallet authenticated: %s", gg._wallet_address)
            else:
                logger.warning("Getgems wallet auth failed — auto-listing disabled")
        except Exception as e:
            logger.warning("Getgems wallet auth error: %s", e)
    else:
        logger.info("No WALLET_MNEMONIC — Getgems auto-listing disabled (use /wallet in bot)")

    # ── 4. Trading services ────────────────────────────────────────────
    redis_url = os.environ.get("REDIS_URL", settings.redis_url) or None
    lock_manager = create_lock_manager(redis_url)

    wash_detector = WashTradeDetector()
    pricing = PricingEngine(wash_detector)
    liquidation = LiquidationModel()
    quality = MarketQualityFilter(wash_detector)
    risk = RiskEngine(
        max_total_exposure=int(settings.max_total_exposure_ton * 1_000_000_000),
        max_per_collection=int(settings.max_per_collection_ton * 1_000_000_000),
        max_items_total=settings.max_items_total,
        max_items_per_collection=settings.max_items_per_collection,
        daily_loss_limit=int(settings.daily_loss_limit_ton * 1_000_000_000),
        max_holding_hours=settings.max_holding_hours,
    )
    circuit = CircuitBreaker()
    notification = NotificationService()
    portfolio = PortfolioManager()

    mds = MarketDataService(
        mrkt_client=mrkt,
        getgems_client=gg,
        pricing_engine=pricing,
        liquidation_model=liquidation,
        fragment_client=fragment,
        portal_client=portal,
    )

    shadow_mode = os.environ.get("SHADOW_MODE", str(settings.shadow_mode)).lower() == "true"

    # Buy callback — calls MRKT API to buy a gift
    async def buy_gift(gift_id: str, price: int) -> bool:
        # Pre-check balance to avoid phantom deals
        try:
            bal = await mrkt.get_balance()
            if bal:
                hard = bal.get("hard", 0)
                if hard < price:
                    from bot.core.exceptions import InsufficientBalanceError
                    raise InsufficientBalanceError(
                        f"balance {hard} < price {price}",
                        required=price,
                        available=hard,
                    )
        except InsufficientBalanceError:
            raise
        except Exception:
            pass  # proceed anyway if balance check fails

        result = await mrkt.buy_gifts([gift_id], {gift_id: price})
        if result is None:
            return False
        if isinstance(result, dict):
            err = result.get("error") or result.get("message")
            if err:
                logger.warning("Buy failed for %s: %s", gift_id, err)
                return False
            if result.get("gifts") or result.get("ok"):
                return True
        logger.warning("Buy unclear response for %s: %s", gift_id, result)
        return False

    execution = ExecutionEngine(
        lock_manager=lock_manager,
        pricing=pricing,
        liquidation=liquidation,
        quality=quality,
        risk=risk,
        circuit=circuit,
        buy_callback=buy_gift,
        shadow_mode=shadow_mode,
    )

    # ── 5. Telegram Bot ────────────────────────────────────────────────
    bot, dp = telegram_bot.create_bot(settings.telegram_bot_token)
    notification.configure(bot.send_message, settings.admin_chat_id)

    # Auth expired callbacks — auto-refresh via Telethon, fallback to user notification
    _gt_ref: list[Any] = []  # mutable ref to GiftTransfer (set after init)

    async def _refresh_via_telethon(bot_username: str, short_name: str) -> str | None:
        """Get fresh TMA init data via Telethon RequestWebView."""
        gt = _gt_ref[0] if _gt_ref else None
        if gt is None or not gt._connected or gt._client is None:
            return None
        from urllib.parse import unquote

        from telethon.tl.functions.messages import RequestAppWebViewRequest, RequestWebViewRequest
        from telethon.tl.types import InputBotAppShortName
        bot_entity = await gt._client.get_input_entity(bot_username)
        try:
            app = InputBotAppShortName(bot_id=bot_entity, short_name=short_name)
            result = await gt._client(RequestAppWebViewRequest(
                peer=bot_entity, app=app, platform="android",
            ))
        except Exception:
            result = await gt._client(RequestWebViewRequest(
                peer=bot_entity, bot=bot_entity, platform="android",
                url=f"https://{bot_username}.xyz",
            ))
        raw_url = result.url
        return unquote(raw_url.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion", 1)[0])

    async def _refresh_mrkt_token() -> bool:
        """Auto-refresh MRKT auth token using Telethon session."""
        try:
            init_data = await _refresh_via_telethon("mrkt", "app")
            if not init_data:
                return False
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(
                    "https://api.tgmrkt.io/api/v1/auth",
                    json={"data": init_data},
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        token = data.get("token", "")
                        if token:
                            mrkt.update_token(token)
                            logger.info("MRKT token auto-refreshed via Telethon")
                            return True
            return False
        except Exception as e:
            logger.warning("MRKT token auto-refresh failed: %s", e)
            return False

    async def _refresh_portal_tma() -> bool:
        """Auto-refresh Portal TMA auth using Telethon session."""
        try:
            tg_data = await _refresh_via_telethon("portals", "market")
            if not tg_data:
                return False
            portal.update_auth(tg_data)
            logger.info("Portal TMA auto-refreshed via Telethon")
            return True
        except Exception as e:
            logger.warning("Portal TMA auto-refresh failed: %s", e)
            return False

    async def on_mrkt_token_expired() -> None:
        refreshed = await _refresh_mrkt_token()
        if not refreshed:
            await notification.notify_system(
                "🔴 <b>MRKT токен истёк!</b>\n\n"
                "Обнови через бот:\n<code>/token новый_токен</code>\n\n"
                "Или проверь статус: /auth"
            )

    async def on_portal_auth_expired() -> None:
        refreshed = await _refresh_portal_tma()
        if not refreshed:
            await notification.notify_system(
                "🔴 <b>Portal TMA токен истёк!</b>\n\n"
                "Обнови через бот:\n<code>/portal tma_init_data</code>\n\n"
                "Получить: откройте @portals_market_bot → "
                "перехватите запрос → Authorization header.\n"
                "Или: /auth"
            )

    async def on_fragment_auth_expired() -> None:
        await notification.notify_system(
            "🔴 <b>Fragment cookies истекли!</b>\n\n"
            "Обнови через бот:\n"
            '<code>/fragment {"stel_token":"...", '
            '"stel_ton_token":"..."}</code>\n\n'
            "Получить: fragment.com → DevTools → Cookies.\n"
            "Или: /auth"
        )

    async def on_getgems_auth_expired() -> None:
        await notification.notify_system(
            "🔴 <b>Getgems API key истёк!</b>\n\n"
            "Обнови через бот:\n<code>/getgems новый_key</code>\n\n"
            "Или: /auth"
        )

    mrkt.set_token_expired_callback(on_mrkt_token_expired)
    portal.set_auth_expired_callback(on_portal_auth_expired)
    gg.set_auth_expired_callback(on_getgems_auth_expired)

    # Fragment callback — set after initialization
    frag_client = mds._fragment if hasattr(mds, "_fragment") else None
    if frag_client:
        frag_client.set_auth_expired_callback(on_fragment_auth_expired)

    # ── 6. Scanner setup (starts empty, loads collections in background) ─
    from bot.orchestration.scanner import AdaptiveScanner

    async def fetch_mrkt(collection: str, market: Market) -> MarketSnapshot | None:
        return await mds.fetch_mrkt_snapshot(collection, market)

    async def fetch_getgems(collection: str, market: Market) -> MarketSnapshot | None:
        return await mds.fetch_getgems_snapshot(collection, market)

    # Throttle: don't spam same collection more than once per 10 min
    _notified_opps: dict[str, float] = {}

    async def _notify_opp(label: str, opp: Any) -> None:
        """Send Telegram notification for opportunity (throttled)."""
        key = f"{label}:{opp.collection}"
        now = __import__("time").time()
        if key in _notified_opps and now - _notified_opps[key] < 600:
            return
        _notified_opps[key] = now
        roi = opp.pnl.roi_pct if opp.pnl else 0
        is_shadow = scanner.shadow_mode if scanner else shadow_mode
        shadow_tag = "👻 " if is_shadow else "🔥 "
        text = (
            f"{shadow_tag}<b>{label}</b>\n\n"
            f"📦 {opp.collection}\n"
            f"💰 Купить: <b>{nanoton_to_ton(opp.listing_price):.2f}</b> TON\n"
            f"💎 Продать: <b>{nanoton_to_ton(opp.target_sell_price):.2f}</b> TON\n"
            f"📈 ROI: <b>{roi:.1f}%</b>"
        )
        try:
            await bot.send_message(settings.admin_chat_id, text, parse_mode="HTML")
        except Exception as e:
            logger.debug("notify_opp error: %s", e)

    async def on_opportunities(snap: MarketSnapshot, prev: MarketSnapshot | None) -> list[dict]:
        """Detect cross-market and intra-market opportunities."""
        all_opps: list[dict] = []

        # Cross-market: MRKT → Getgems/Portal (picks best sell target)
        if snap.market == Market.MRKT and settings.cross_market_enabled:
            gg_floor = (
                (snap.getgems_floor or mds.get_cached_gg_floor(snap.collection))
                if telegram_bot.is_market_sell_enabled("getgems")
                else 0
            )
            frag_floor = (
                mds.get_cached_fragment_floor(snap.collection)
                if telegram_bot.is_market_sell_enabled("fragment")
                else 0
            )
            portal_floor = (
                mds.get_cached_portal_floor(snap.collection)
                if telegram_bot.is_market_sell_enabled("portal")
                else 0
            )
            _sm = scanner.shadow_mode
            cross_opps = mds.find_cross_market_opportunities(
                snap,
                gg_floor,
                _sm,
                fragment_floor=frag_floor,
                portal_floor=portal_floor,
            )
            for opp in cross_opps:
                sell_labels = {
                    Market.GETGEMS: "Getgems",
                    Market.PORTAL: "Portal",
                    Market.FRAGMENT: "Fragment",
                }
                sell_label = sell_labels.get(opp.sell_market, str(opp.sell_market))
                logger.info(
                    "%sCROSS_MARKET: %s buy=%.2f sell=%.2f ROI=%.1f%% → %s",
                    "👻 " if _sm else "",
                    opp.collection,
                    nanoton_to_ton(opp.listing_price),
                    nanoton_to_ton(opp.target_sell_price),
                    opp.pnl.roi_pct if opp.pnl else 0,
                    sell_label,
                )
                await _notify_opp(f"Кросс-маркет (MRKT→{sell_label})", opp)
                deal_id = await execution.execute_opportunity(opp, snap)
                if deal_id:
                    logger.info("EXEC OK: deal_id=%s %s", deal_id, opp.collection)
                    all_opps.append({"deal_id": deal_id, "type": "cross_market"})
                else:
                    logger.info("EXEC SKIP: %s (shadow=%s)", opp.collection, execution.shadow_mode)

        # Order arbitrage
        if snap.market == Market.MRKT and settings.order_arb_enabled:
            order_opps = mds.find_order_arb_opportunities(snap)
            for opp in order_opps:
                logger.info(
                    "%sORDER_ARB: %s buy=%.2f fill=%.2f ROI=%.1f%%",
                    "👻 " if scanner.shadow_mode else "",
                    opp.collection,
                    nanoton_to_ton(opp.listing_price),
                    nanoton_to_ton(opp.target_sell_price),
                    opp.pnl.roi_pct if opp.pnl else 0,
                )
                await _notify_opp("Ордер-арбитраж", opp)
                deal_id = await execution.execute_opportunity(opp, snap)
                if deal_id:
                    all_opps.append({"deal_id": deal_id, "type": "order_arb"})

        # Deep discount
        if snap.market == Market.MRKT and settings.deep_discount_enabled:
            deep_opps = mds.find_deep_discount_opportunities(snap)
            for opp in deep_opps:
                logger.info(
                    "%sDEEP_DISCOUNT: %s buy=%.2f fair=%.2f ROI=%.1f%%",
                    "👻 " if scanner.shadow_mode else "",
                    opp.collection,
                    nanoton_to_ton(opp.listing_price),
                    nanoton_to_ton(opp.target_sell_price),
                    opp.pnl.roi_pct if opp.pnl else 0,
                )
                await _notify_opp("Скидка ниже рынка", opp)
                deal_id = await execution.execute_opportunity(opp, snap)
                if deal_id:
                    all_opps.append({"deal_id": deal_id, "type": "deep_discount"})

        return all_opps

    scanner = AdaptiveScanner(
        collections=[],
        fetch_mrkt=fetch_mrkt,
        fetch_getgems=fetch_getgems,
        on_opportunities=on_opportunities,
        shadow_mode=shadow_mode,
        runtime_getter=telegram_bot.get_runtime,
        mds=mds,
    )

    # ── 7. Auto-seller ──────────────────────────────────────────────────
    from bot.execution.auto_seller import AutoSeller

    auto_seller = AutoSeller(
        mrkt=mrkt,
        mds=mds,
        admin_chat_id=settings.admin_chat_id,
        bot=bot,
        fragment=fragment,
        check_interval=120.0,
        enabled=True,
        shadow_mode=settings.shadow_mode,
    )

    # Wire auto_seller.mark_bought into execution engine
    execution._on_bought = auto_seller.mark_bought

    # ── 8. Gift transfer ─────────────────────────────────────────────────
    from bot.gift_transfer import GiftTransfer

    gift_transfer = GiftTransfer(
        session_string=settings.telegram_session,
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        targets={
            "getgems": settings.gift_target_getgems,
            "mrkt": settings.gift_target_mrkt,
            "fragment": settings.gift_target_fragment,
        },
        check_interval=300.0,
        two_fa_password=os.environ.get("TELEGRAM_2FA_PASSWORD", ""),
    )
    if settings.telegram_session:
        try:
            await gift_transfer.connect()
        except Exception as e:
            logger.warning("GiftTransfer connect failed: %s", e)

    # Wire notification callback
    async def _gift_notify(msg: str) -> None:
        await notification.notify_system(msg)

    gift_transfer.set_notify(_gift_notify)

    # Connect gift_transfer to auto_seller and market data
    auto_seller._gift_transfer = gift_transfer
    gift_transfer._mds = mds

    # Wire Portal TMA auto-refresh to use gift_transfer's Telethon session
    _gt_ref.append(gift_transfer)

    telegram_bot.configure(
        admin_chat_id=settings.admin_chat_id,
        scanner=scanner,
        circuit=circuit,
        risk=risk,
        mds=mds,
        mrkt=mrkt,
        gg=gg,
    )
    # Pass auto_seller, execution engine, and gift transfer
    telegram_bot._state["auto_seller"] = auto_seller
    telegram_bot._state["execution"] = execution
    telegram_bot._state["gift_transfer"] = gift_transfer
    telegram_bot._state["fragment_client"] = fragment
    telegram_bot._state["portal_client"] = portal

    # ── Statistics service ─────────────────────────────────────────────
    from bot.statistics import StatisticsService

    statistics = StatisticsService(
        mrkt_client=mrkt,
        gg_client=gg,
        fragment_client=fragment,
        mds=mds,
    )
    telegram_bot._state["statistics"] = statistics

    logger.info("System ready: shadow=%s, autosell=ON", shadow_mode)

    # ── 9. Background tasks ────────────────────────────────────────────

    async def initial_loader() -> None:
        """Load collections and Getgems floors in background (doesn't block bot)."""
        await asyncio.sleep(5)  # let bot start first

        for attempt in range(10):
            try:
                collections = await mds.load_collections()
                if collections:
                    scanner.update_collections(collections)
                    logger.info("Scanner updated with %d collections", len(collections))
                    await notification.notify_system(
                        f"🤖 Система запущена\n"
                        f"👻 Теневой режим: {'ВКЛ' if shadow_mode else 'ВЫКЛ'}\n"
                        f"📂 Коллекций: {len(collections)}\n"
                        f"🏷 Автопродажа: ВКЛ"
                    )
                    # Load Getgems floors
                    try:
                        await mds.refresh_getgems_floors(collections)
                    except Exception:
                        logger.warning("Initial Getgems refresh failed")
                    # Load Fragment floors (skip numeric gift IDs)
                    if fragment.ready:
                        try:
                            real_collections = [c for c in collections if not c.isdigit()]
                            await mds.refresh_fragment_floors(real_collections)
                        except Exception:
                            logger.warning("Initial Fragment refresh failed")
                    # Load Portal floors
                    try:
                        await mds.refresh_portal_floors()
                    except Exception:
                        logger.warning("Initial Portal refresh failed")
                    return
            except Exception as e:
                logger.warning("Collections load attempt %d failed: %s", attempt + 1, e)
            wait = min(30 * (attempt + 1), 180)
            logger.info("Retrying collections in %ds...", wait)
            await asyncio.sleep(wait)

        logger.error("Failed to load collections after 10 attempts")
        await notification.notify_system("⚠️ Не удалось загрузить коллекции после 10 попыток")

    async def getgems_floor_updater() -> None:
        """Periodically refresh Getgems floor prices."""
        await asyncio.sleep(60)
        while True:
            try:
                if scanner._running:
                    colls = list(scanner._priorities.keys())
                    if colls:
                        await mds.refresh_getgems_floors(colls)
            except Exception:
                logger.exception("Getgems floor update error")
            await asyncio.sleep(120)

    async def fragment_floor_updater() -> None:
        """Periodically refresh Fragment floor prices."""
        await asyncio.sleep(90)
        while True:
            if not fragment.ready:
                await asyncio.sleep(300)
                continue
            try:
                if scanner._running:
                    colls = [c for c in scanner._priorities.keys() if not c.isdigit()]
                    if colls:
                        await mds.refresh_fragment_floors(colls)
            except Exception:
                logger.exception("Fragment floor update error")
            await asyncio.sleep(180)

    async def portal_floor_updater() -> None:
        """Periodically refresh Portal floor prices (1 request for all collections)."""
        await asyncio.sleep(45)  # wait for initial load
        while True:
            try:
                await mds.refresh_portal_floors()
            except Exception:
                logger.exception("Portal bulk floor update error")
            await asyncio.sleep(30)  # every 30 seconds

    async def mrkt_floor_updater() -> None:
        """Periodically refresh MRKT bulk floors (1 request for all collections)."""
        await asyncio.sleep(30)  # wait for initial load
        while True:
            try:
                await mds.refresh_mrkt_floors_bulk()
            except Exception:
                logger.exception("MRKT bulk floor update error")
            await asyncio.sleep(30)  # every 30 seconds

    async def stale_deal_cleaner() -> None:
        """Periodically check for stale deals and close them."""
        await asyncio.sleep(120)
        owner_tg_id = settings.admin_chat_id
        while True:
            try:
                closed = await portfolio.cleanup_stale_deals(
                    mrkt_client=mrkt,
                    owner_tg_id=owner_tg_id,
                )
                if closed:
                    msg = f"🧹 Закрыто {len(closed)} зависших сделок:\n"
                    for c in closed[:10]:
                        msg += (
                            f"  • {c['collection']}"
                            f" ({c['buy_price_ton']:.1f} TON)"
                            f" — {c['reason']}\n"
                        )
                    await notification.notify_system(msg)
            except Exception:
                logger.exception("Stale deal cleanup error")
            await asyncio.sleep(1800)  # every 30 min

    async def inventory_monitor() -> None:
        """Periodically scan all markets for inventory changes (manual trades)."""
        await asyncio.sleep(60)
        owner_tg_id = settings.admin_chat_id
        while True:
            try:
                changes = await statistics.run_inventory_scan(
                    owner_tg_id=owner_tg_id,
                    gift_transfer=gift_transfer,
                )
                for ch in changes:
                    action_label = {
                        "buy": "🟢 Покупка",
                        "sell": "🔴 Продажа",
                        "transfer_in": "📥 Получен",
                        "transfer_out": "📤 Передан",
                    }.get(ch["action"], ch["action"])
                    price_str = f" — {ch['price'] / 1e9:.1f} TON" if ch.get("price") else ""
                    msg = f"📊 {action_label} ({ch['market'].upper()})\n🎁 {ch['name']}{price_str}"
                    await notification.notify_system(msg)
            except Exception:
                logger.exception("Inventory monitor error")
            await asyncio.sleep(300)  # every 5 min

    async def system_heartbeat() -> None:
        """Log system health every 5 minutes."""
        await asyncio.sleep(60)
        while True:
            try:
                logger.info(
                    "heartbeat: scanner=%s shadow=%s auto_sell=%s colls=%d",
                    scanner._running,
                    scanner.shadow_mode,
                    auto_seller.enabled,
                    len(scanner._priorities),
                )
            except Exception:
                pass
            await asyncio.sleep(300)

    async def balance_tracker() -> None:
        """Periodically record balance snapshots."""
        await asyncio.sleep(180)  # wait for systems to start
        while True:
            try:
                bal = await statistics.collect_balance()
                logger.info(
                    "Balance snapshot: wallet=%.1f, mrkt=%.1f, gifts=%.1f, total=%.1f TON",
                    bal["wallet"] / 1e9,
                    bal["mrkt"] / 1e9,
                    bal["gifts"] / 1e9,
                    bal["total"] / 1e9,
                )
            except Exception:
                logger.exception("Balance tracker error")
            await asyncio.sleep(1800)  # every 30 min

    # ── 10. Graceful shutdown ─────────────────────────────────────────
    import signal

    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_event.set)

    async def _shutdown_watcher() -> None:
        await shutdown_event.wait()
        logger.info("Shutdown initiated — stopping all tasks gracefully")
        scanner.stop()
        auto_seller.stop()
        # Stop aiogram polling gracefully
        try:
            if dp._stop_signal is not None:
                dp._stop_signal.set()
        except Exception:
            pass
        # Give active operations time to complete
        logger.info("Waiting 5s for active operations to complete...")
        await asyncio.sleep(5)
        # Cancel remaining tasks
        current = asyncio.current_task()
        for t in asyncio.all_tasks():
            if t is not current and not t.done():
                t.cancel()
        logger.info("Shutdown watcher: all tasks cancelled")

    # ── 11. Run ────────────────────────────────────────────────────────
    try:
        tasks = [
            dp.start_polling(
                bot, handle_signals=False, close_bot_session=False,
            ),
            initial_loader(),  # collections load in background
            scanner.start(),
            mrkt_floor_updater(),  # bulk MRKT floors every 30s
            getgems_floor_updater(),
            fragment_floor_updater(),
            portal_floor_updater(),  # bulk Portal floors every 30s
            auto_seller.run(),
            stale_deal_cleaner(),
            inventory_monitor(),
            balance_tracker(),
            system_heartbeat(),
            _shutdown_watcher(),
        ]
        if gift_transfer.ready:
            tasks.append(gift_transfer.run())
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Log which tasks finished and why
        task_names = [
            "polling", "loader", "scanner", "mrkt_floors", "gg_floors",
            "frag_floors", "portal_floors", "auto_seller", "stale_cleaner",
            "inventory", "balance", "heartbeat", "shutdown_watcher",
        ]
        for name, result in zip(task_names, results):
            if isinstance(result, BaseException):
                logger.error("Task '%s' crashed: %s", name, result)
            elif result is not None:
                logger.info("Task '%s' returned: %s", name, result)
    finally:
        auto_seller.stop()
        await gift_transfer.disconnect()
        await mrkt.close()
        await gg.close()
        await fragment.close()
        await portal.close()
        try:
            await bot.session.close()
        except Exception:
            pass
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
