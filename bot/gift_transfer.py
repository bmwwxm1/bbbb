"""Gift transfer via Telethon — auto-send unique gifts between Telegram accounts.

Background loop checks for transferable gifts every few minutes.
When a gift's hold expires, it's automatically sent to the configured target.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest
from telethon.tl.functions.payments import (
    GetSavedStarGiftsRequest,
    GetStarGiftWithdrawalUrlRequest,
    TransferStarGiftRequest,
)
from telethon.tl.types import (
    InputPeerSelf,
    InputSavedStarGiftSlug,
    InputSavedStarGiftUser,
)

logger = logging.getLogger(__name__)

# Callback type: async fn(message: str) -> None
NotifyFn = Callable[[str], Awaitable[None]]


class GiftTransfer:
    """Manages automatic gift transfers between Telegram accounts."""

    def __init__(
        self,
        session_string: str,
        api_id: int = 2040,
        api_hash: str = "b18441a1ff607e10a989891a5462e627",
        targets: dict[str, str] | None = None,
        check_interval: float = 300.0,
        two_fa_password: str = "",
    ) -> None:
        self._session_string = session_string
        self._api_id = api_id
        self._api_hash = api_hash
        # market -> target username
        self._targets: dict[str, str] = targets or {}
        self._check_interval = check_interval
        self._two_fa_password = two_fa_password
        self._client: TelegramClient | None = None
        self._connected = False
        self._me: Any = None
        self._target_cache: dict[str, Any] = {}
        self._running = False
        self._notify_fn: NotifyFn | None = None
        self._mds: Any = None  # MarketDataService, set after init

        # Tracking
        self._transferred: list[dict[str, Any]] = []
        self._pending_holds: dict[str, int] = {}  # slug -> can_transfer_at
        self._stats = {"checks": 0, "transfers": 0, "errors": 0}

    @property
    def ready(self) -> bool:
        return self._connected and self._client is not None

    def set_notify(self, fn: NotifyFn) -> None:
        self._notify_fn = fn

    async def _notify(self, msg: str) -> None:
        if self._notify_fn:
            try:
                await self._notify_fn(msg)
            except Exception:
                pass

    async def connect(self) -> bool:
        if not self._session_string:
            logger.warning("GiftTransfer: no session string configured")
            return False
        try:
            self._client = TelegramClient(
                StringSession(self._session_string),
                self._api_id,
                self._api_hash,
            )
            await self._client.connect()
            self._me = await self._client.get_me()
            self._connected = True
            logger.info(
                "GiftTransfer connected: %s (@%s, id=%s)",
                self._me.first_name,
                self._me.username,
                self._me.id,
            )
            return True
        except Exception as e:
            logger.error("GiftTransfer connect failed: %s", e)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._client:
            await self._client.disconnect()
            self._connected = False

    async def get_my_gifts(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.ready or not self._client:
            return []
        try:
            result = await self._client(
                GetSavedStarGiftsRequest(
                    peer=InputPeerSelf(),
                    offset="",
                    limit=limit,
                )
            )
            gifts = []
            for sg in result.gifts:
                gift = sg.gift
                gift_type = type(gift).__name__
                info: dict[str, Any] = {
                    "msg_id": sg.msg_id,
                    "saved_id": sg.saved_id,
                    "date": sg.date,
                    "type": gift_type,
                    "transfer_stars": sg.transfer_stars,
                    "can_transfer_at": sg.can_transfer_at,
                    "can_export_at": sg.can_export_at,
                    "is_unique": gift_type == "StarGiftUnique",
                }
                if hasattr(gift, "title"):
                    info["title"] = gift.title
                if hasattr(gift, "slug"):
                    info["slug"] = gift.slug
                if hasattr(gift, "id"):
                    info["gift_id"] = gift.id
                if hasattr(gift, "num"):
                    info["num"] = gift.num

                now = int(time.time())
                can_at = sg.can_transfer_at or 0
                info["transferable"] = info["is_unique"] and (can_at == 0 or can_at <= now)
                info["hold_seconds"] = max(0, can_at - now) if can_at > 0 else 0

                gifts.append(info)
            return gifts
        except Exception as e:
            logger.error("GiftTransfer get_my_gifts error: %s", e)
            return []

    async def _resolve_target(self, username: str) -> Any:
        if not self._client:
            return None
        username = username.lstrip("@")
        if username in self._target_cache:
            return self._target_cache[username]
        target = await self._client.get_entity(username)
        self._target_cache[username] = target
        return target

    async def transfer_gift(
        self,
        msg_id: int | None = None,
        slug: str | None = None,
        target_username: str = "",
    ) -> dict[str, Any]:
        if not self.ready or not self._client:
            return {"success": False, "error": "not connected"}
        if not target_username:
            return {"success": False, "error": "no target username"}

        target_username = target_username.lstrip("@")

        try:
            target = await self._resolve_target(target_username)
        except Exception as e:
            return {"success": False, "error": f"resolve @{target_username}: {e}"}

        if msg_id is not None:
            gift_input = InputSavedStarGiftUser(msg_id=msg_id)
        elif slug is not None:
            gift_input = InputSavedStarGiftSlug(slug=slug)
        else:
            return {"success": False, "error": "provide msg_id or slug"}

        try:
            await self._client(
                TransferStarGiftRequest(
                    stargift=gift_input,
                    to_id=target,
                )
            )
            logger.info("Gift transferred: %s → @%s", slug or msg_id, target_username)
            self._stats["transfers"] += 1
            return {"success": True, "error": None}
        except Exception as e:
            err = str(e)
            logger.error("Gift transfer failed: %s → @%s: %s", slug or msg_id, target_username, err)
            self._stats["errors"] += 1
            return {"success": False, "error": err}

    async def export_to_nft(
        self,
        msg_id: int | None = None,
        slug: str | None = None,
    ) -> dict[str, Any]:
        """Export a Telegram gift to NFT on TON blockchain.

        Requires 2FA password. After export, the gift becomes an NFT
        that can be listed on Fragment via getStartAuctionLink.
        """
        if not self.ready or not self._client:
            return {"success": False, "error": "not connected"}
        if not self._two_fa_password:
            return {"success": False, "error": "2FA password not configured"}

        if msg_id is not None:
            gift_input = InputSavedStarGiftUser(msg_id=msg_id)
        elif slug is not None:
            gift_input = InputSavedStarGiftSlug(slug=slug)
        else:
            return {"success": False, "error": "provide msg_id or slug"}

        try:
            from telethon.password import compute_check

            pwd = await self._client(GetPasswordRequest())
            srp_check = compute_check(pwd, self._two_fa_password)

            result = await self._client(
                GetStarGiftWithdrawalUrlRequest(
                    stargift=gift_input,
                    password=srp_check,
                )
            )
            url = getattr(result, "url", str(result))
            logger.info("Gift exported to NFT: %s → %s", slug or msg_id, url)
            return {"success": True, "url": url, "error": None}
        except Exception as e:
            err = str(e)
            logger.error("Gift export to NFT failed: %s: %s", slug or msg_id, err)
            return {"success": False, "error": err}

    def _find_best_target(self, gift_name: str) -> tuple[str, str]:
        """Determine the best market to sell on and return (market_name, target_username).

        Compares MRKT and Getgems floor prices (Fragment excluded — same account).
        """
        mrkt_target = self._targets.get("mrkt", "")
        gg_target = self._targets.get("getgems", "")

        if not self._mds:
            return ("Getgems", gg_target) if gg_target else ("MRKT", mrkt_target)

        # Extract base collection name from title like "Vice Cream #305997"
        coll_name = gift_name.rsplit("#", 1)[0].strip() if "#" in gift_name else gift_name

        mrkt_floor = 0
        gg_floor = 0
        try:
            mrkt_floors = self._mds.get_all_mrkt_floors()
            gg_floors = (
                self._mds.get_all_gg_floors() if hasattr(self._mds, "get_all_gg_floors") else {}
            )
            for name, price in mrkt_floors.items():
                if name.lower().rstrip("s") == coll_name.lower().rstrip("s"):
                    mrkt_floor = price
                    break
            for name, price in gg_floors.items():
                if name.lower().rstrip("s") == coll_name.lower().rstrip("s"):
                    gg_floor = price
                    break
        except Exception:
            pass

        # Account for withdrawal fees (nanoton)
        NANO = 1_000_000_000
        mrkt_net = mrkt_floor - int(0.2 * NANO) if mrkt_floor > 0 else 0
        gg_net = gg_floor - int(0.3 * NANO) if gg_floor > 0 else 0

        if gg_net >= mrkt_net and gg_target:
            return ("Getgems", gg_target)
        elif mrkt_net > 0 and mrkt_target:
            return ("MRKT", mrkt_target)
        elif gg_target:
            return ("Getgems", gg_target)
        return ("MRKT", mrkt_target)

    def get_target_for_market(self, market: str) -> str:
        return self._targets.get(market, "")

    async def run(self) -> None:
        """Background loop — auto-transfer gifts when holds expire."""
        self._running = True
        logger.info("GiftTransfer auto-loop started (interval=%.0fs)", self._check_interval)

        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                if not self.ready:
                    continue

                self._stats["checks"] += 1
                gifts = await self.get_my_gifts(limit=50)
                unique_gifts = [g for g in gifts if g["is_unique"]]

                if not unique_gifts:
                    continue

                for g in unique_gifts:
                    slug = g.get("slug", "")
                    title = g.get("title", slug)

                    if not g["transferable"]:
                        hold = g["hold_seconds"]
                        if slug and slug not in self._pending_holds:
                            self._pending_holds[slug] = g.get("can_transfer_at", 0)
                            hours = hold / 3600
                            best_market, best_target = self._find_best_target(title)
                            target_line = ""
                            if best_target:
                                target_line = (
                                    f"\n➡️ <b>Переведи на @{best_target}</b> ({best_market})"
                                )
                            await self._notify(
                                f"⏳ <b>Подарок на холде</b>\n\n"
                                f"🎁 <b>{title}</b>\n"
                                f"⏰ Передача через: <b>{hours:.1f}ч</b>\n"
                                f"📋 Автопередача включена"
                                f"{target_line}"
                            )
                        continue

                    # Gift is transferable — find best target
                    best_market, target = self._find_best_target(title)
                    if not target:
                        await self._notify(
                            f"⚠️ <b>Подарок готов к передаче, но нет целевого аккаунта!</b>\n\n"
                            f"🎁 <b>{title}</b>\n"
                            f"❌ Настройте GIFT_TARGET_GETGEMS или GIFT_TARGET_MRKT"
                        )
                        continue

                    # Auto-transfer
                    result = await self.transfer_gift(
                        msg_id=g["msg_id"],
                        slug=slug,
                        target_username=target,
                    )

                    if result["success"]:
                        self._transferred.append(
                            {
                                "title": title,
                                "slug": slug,
                                "target": target,
                                "time": time.time(),
                            }
                        )
                        self._pending_holds.pop(slug, None)
                        await self._notify(
                            f"🎁🎁🎁 <b>ПОДАРОК ПЕРЕДАН!</b> 🎁🎁🎁\n\n"
                            f"📦 <b>{title}</b>\n"
                            f"➡️ Передан на: <b>@{target}</b>\n"
                            f"✅ Автоматически после снятия холда\n\n"
                            f"💡 Теперь выставьте на продажу на площадке"
                        )
                    else:
                        err = result.get("error", "unknown")
                        await self._notify(
                            f"❌ <b>ОШИБКА ПЕРЕДАЧИ</b>\n\n"
                            f"🎁 <b>{title}</b>\n"
                            f"➡️ Цель: @{target}\n"
                            f"⚠️ {err}\n\n"
                            f"🔄 Повторю через {self._check_interval / 60:.0f} мин"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("GiftTransfer loop error: %s", e)
                await asyncio.sleep(60)

        logger.info("GiftTransfer auto-loop stopped")

    def get_status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "running": self._running,
            "user": f"@{self._me.username}" if self._me else None,
            "user_id": self._me.id if self._me else None,
            "targets": dict(self._targets),
            "stats": dict(self._stats),
            "pending_holds": len(self._pending_holds),
            "transferred_total": len(self._transferred),
            "last_transfers": self._transferred[-5:],
        }
