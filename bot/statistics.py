"""Statistics service — comprehensive P&L tracking across all markets.

Tracks:
  - Auto trades (from deals DB)
  - Manual trades (detected by inventory monitoring)
  - Balance snapshots (periodic)
  - Multi-market inventory (MRKT, Getgems, Fragment, Portal)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from bot.models.database import (
    BalanceSnapshot,
    Deal,
    ManualTrade,
    async_session,
)
from bot.models.types import TERMINAL_STATES, DealState

logger = logging.getLogger(__name__)

NANOTON = 1_000_000_000

# Fee rates by market
MARKET_FEES: dict[str, float] = {
    "mrkt": 0.05,  # 5%
    "getgems": 0.0,  # 0% for gifts
    "fragment": 0.05,  # 5%
    "portal": 0.0,  # 0%
}
WITHDRAWAL_FEES: dict[str, int] = {
    "mrkt": int(0.2 * NANOTON),
    "getgems": int(0.3 * NANOTON),
    "fragment": 0,
    "portal": int(0.25 * NANOTON),
}


class StatisticsService:
    """Aggregates statistics from deals + manual trades + balance snapshots."""

    def __init__(
        self,
        mrkt_client: Any = None,
        gg_client: Any = None,
        fragment_client: Any = None,
        mds: Any = None,
    ) -> None:
        self._mrkt = mrkt_client
        self._gg = gg_client
        self._fragment = fragment_client
        self._mds = mds

        # Inventory tracking — {gift_id: {name, collection, market, ...}}
        self._known_mrkt_gifts: dict[str, dict[str, Any]] = {}
        self._known_gg_gifts: dict[str, dict[str, Any]] = {}
        self._known_tg_gifts: dict[str, dict[str, Any]] = {}

    # ── Balance Snapshots ──────────────────────────────────────────────

    async def record_balance_snapshot(
        self,
        wallet_balance: int = 0,
        mrkt_balance: int = 0,
        gifts_value: int = 0,
        gifts_count: int = 0,
        note: str | None = None,
    ) -> None:
        total = wallet_balance + mrkt_balance + gifts_value
        async with async_session() as session:
            snap = BalanceSnapshot(
                wallet_balance=wallet_balance,
                mrkt_balance=mrkt_balance,
                gifts_value=gifts_value,
                gifts_count=gifts_count,
                total_value=total,
                note=note,
            )
            session.add(snap)
            await session.commit()

    async def get_balance_history(self, days: int = 30) -> list[dict[str, Any]]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with async_session() as session:
            result = await session.execute(
                select(BalanceSnapshot)
                .where(BalanceSnapshot.timestamp >= since)
                .order_by(BalanceSnapshot.timestamp.asc())
            )
            snaps = result.scalars().all()

        return [
            {
                "timestamp": s.timestamp,
                "wallet": s.wallet_balance,
                "mrkt": s.mrkt_balance,
                "gifts": s.gifts_value,
                "gifts_count": s.gifts_count,
                "total": s.total_value,
            }
            for s in snaps
        ]

    async def get_first_balance(self) -> dict[str, Any] | None:
        async with async_session() as session:
            result = await session.execute(
                select(BalanceSnapshot).order_by(BalanceSnapshot.timestamp.asc()).limit(1)
            )
            snap = result.scalar_one_or_none()
        if not snap:
            return None
        return {
            "timestamp": snap.timestamp,
            "total": snap.total_value,
            "wallet": snap.wallet_balance,
        }

    # ── Manual Trade Recording ─────────────────────────────────────────

    async def record_manual_trade(
        self,
        gift_name: str,
        collection: str,
        action: str,
        market: str,
        price_nanoton: int,
        gift_id: str = "",
        note: str = "",
    ) -> ManualTrade:
        fee_rate = MARKET_FEES.get(market, 0)
        fee = int(price_nanoton * fee_rate) if action == "sell" else 0
        async with async_session() as session:
            trade = ManualTrade(
                gift_name=gift_name,
                collection=collection,
                action=action,
                market=market,
                price_nanoton=price_nanoton,
                fee_nanoton=fee,
                gift_id=gift_id,
                note=note,
            )
            session.add(trade)
            await session.commit()
            await session.refresh(trade)
        return trade

    async def get_manual_trades(self, days: int = 30) -> list[dict[str, Any]]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        async with async_session() as session:
            result = await session.execute(
                select(ManualTrade)
                .where(ManualTrade.timestamp >= since)
                .order_by(ManualTrade.timestamp.desc())
            )
            trades = result.scalars().all()

        return [
            {
                "id": t.id,
                "timestamp": t.timestamp,
                "gift_name": t.gift_name,
                "collection": t.collection,
                "action": t.action,
                "market": t.market,
                "price_ton": t.price_nanoton / NANOTON,
                "fee_ton": t.fee_nanoton / NANOTON,
                "note": t.note,
            }
            for t in trades
        ]

    # ── Comprehensive Stats ────────────────────────────────────────────

    async def get_full_stats(self, days: int = 30) -> dict[str, Any]:
        """Get complete statistics including auto + manual trades."""
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # 1. Auto deals (from deals table)
        async with async_session() as session:
            # Sold deals
            result = await session.execute(
                select(Deal).where(
                    Deal.state == DealState.SOLD.value,
                    Deal.sold_at >= since,
                    Deal.is_shadow.is_(False),
                )
            )
            sold_deals = result.scalars().all()

            # Active deals
            active_states = [s.value for s in DealState if s not in TERMINAL_STATES]
            result = await session.execute(
                select(Deal).where(
                    Deal.state.in_(active_states),
                    Deal.is_shadow.is_(False),
                )
            )
            active_deals = result.scalars().all()

            # All deals in period
            result = await session.execute(
                select(Deal).where(
                    Deal.detected_at >= since,
                    Deal.is_shadow.is_(False),
                )
            )
            result.scalars().all()

        # 2. Manual trades
        manual = await self.get_manual_trades(days)

        # 3. Calculate auto P&L
        auto_profit = 0
        auto_cost = 0
        auto_revenue = 0
        auto_fees = 0
        auto_winners = 0
        auto_losers = 0
        best_deal: dict[str, Any] | None = None
        worst_deal: dict[str, Any] | None = None
        sell_times: list[float] = []

        # By market route
        by_route: dict[str, dict[str, Any]] = {}

        for d in sold_deals:
            buy = d.buy_price
            sell = d.actual_sell_price or d.sell_price or d.target_sell_price or 0
            profit = d.actual_net_profit or (sell - buy) if sell > 0 else 0

            # Estimate fees
            sell_market = d.sell_market or "getgems"
            fee_rate = MARKET_FEES.get(sell_market, 0.05)
            fee = int(sell * fee_rate) + WITHDRAWAL_FEES.get(sell_market, 0)
            if profit == (sell - buy) and sell > 0:
                profit = sell - buy - fee
            auto_fees += fee

            auto_cost += buy
            auto_revenue += sell
            auto_profit += profit

            if profit > 0:
                auto_winners += 1
            else:
                auto_losers += 1

            deal_info = {
                "collection": d.collection_name,
                "profit_ton": profit / NANOTON,
                "roi_pct": (profit / buy * 100) if buy > 0 else 0,
                "buy_ton": buy / NANOTON,
                "sell_ton": sell / NANOTON,
            }

            if best_deal is None or profit > (best_deal.get("_raw_profit", 0)):
                best_deal = {**deal_info, "_raw_profit": profit}
            if worst_deal is None or profit < (worst_deal.get("_raw_profit", 0)):
                worst_deal = {**deal_info, "_raw_profit": profit}

            # Sell time
            if d.bought_at and d.sold_at:
                hours = (d.sold_at - d.bought_at).total_seconds() / 3600
                sell_times.append(hours)

            # Route stats
            route = f"{d.buy_market or 'mrkt'}→{sell_market}"
            if route not in by_route:
                by_route[route] = {"deals": 0, "profit_ton": 0, "cost_ton": 0}
            by_route[route]["deals"] += 1
            by_route[route]["profit_ton"] += profit / NANOTON
            by_route[route]["cost_ton"] += buy / NANOTON

        # 4. Manual P&L
        manual_buys = sum(t["price_ton"] for t in manual if t["action"] == "buy")
        manual_sells = sum(t["price_ton"] for t in manual if t["action"] == "sell")
        manual_fees = sum(t["fee_ton"] for t in manual if t["action"] == "sell")
        manual_profit = manual_sells - manual_buys - manual_fees
        manual_count = len(manual)

        # 5. Active portfolio unrealized P&L
        unrealized = 0
        active_exposure = 0
        active_by_coll: dict[str, dict[str, Any]] = {}

        for d in active_deals:
            buy = d.buy_price
            active_exposure += buy
            coll = d.collection_name

            # Get current floor for unrealized calc
            floor = 0
            if self._mds:
                mrkt_floors = self._mds.get_all_mrkt_floors()
                gg_floors = self._mds.get_all_gg_floors()
                mrkt_fl = mrkt_floors.get(coll, 0)
                gg_fl = 0
                for k, v in gg_floors.items():
                    if k.lower() == coll.lower() or coll.lower() in k.lower():
                        gg_fl = v
                        break
                floor = max(mrkt_fl, gg_fl)

            # Unrealized = floor * 0.95 (realistic sell) - buy_price
            if floor > 0:
                net_sell = int(floor * 0.95)
                unrealized += net_sell - buy

            if coll not in active_by_coll:
                active_by_coll[coll] = {
                    "items": 0,
                    "cost_ton": 0,
                    "floor_ton": floor / NANOTON if floor > 0 else 0,
                }
            active_by_coll[coll]["items"] += 1
            active_by_coll[coll]["cost_ton"] += buy / NANOTON

        # 6. Balance history
        first = await self.get_first_balance()
        latest_snaps = await self.get_balance_history(days=1)
        latest_total = latest_snaps[-1]["total"] if latest_snaps else 0

        # 7. Speed stats
        avg_sell_time = sum(sell_times) / len(sell_times) if sell_times else 0
        min_sell_time = min(sell_times) if sell_times else 0
        max_sell_time = max(sell_times) if sell_times else 0

        # Clean up internal fields
        if best_deal:
            best_deal.pop("_raw_profit", None)
        if worst_deal:
            worst_deal.pop("_raw_profit", None)

        total_auto = len(sold_deals)
        total_all = total_auto + manual_count
        total_profit = auto_profit / NANOTON + manual_profit

        return {
            "period_days": days,
            # Overall
            "total_deals": total_all,
            "auto_deals": total_auto,
            "manual_deals": manual_count,
            "total_profit_ton": total_profit,
            "auto_profit_ton": auto_profit / NANOTON,
            "manual_profit_ton": manual_profit,
            # Win/Loss
            "winners": auto_winners,
            "losers": auto_losers,
            "win_rate_pct": (auto_winners / total_auto * 100) if total_auto > 0 else 0,
            "avg_roi_pct": (auto_profit / auto_cost * 100) if auto_cost > 0 else 0,
            # Best/Worst
            "best_deal": best_deal,
            "worst_deal": worst_deal,
            # By route
            "by_route": by_route,
            # Fees
            "total_fees_ton": auto_fees / NANOTON + manual_fees,
            "auto_fees_ton": auto_fees / NANOTON,
            "manual_fees_ton": manual_fees,
            # Speed
            "avg_sell_hours": avg_sell_time,
            "min_sell_hours": min_sell_time,
            "max_sell_hours": max_sell_time,
            # Active portfolio
            "active_items": len(active_deals),
            "active_exposure_ton": active_exposure / NANOTON,
            "unrealized_pnl_ton": unrealized / NANOTON,
            "active_by_collection": active_by_coll,
            # Balance
            "initial_balance_ton": first["total"] / NANOTON if first else 0,
            "initial_date": first["timestamp"].strftime("%d.%m.%Y") if first else "н/д",
            "latest_total_ton": latest_total / NANOTON,
            # Manual trade details
            "manual_buys_ton": manual_buys,
            "manual_sells_ton": manual_sells,
        }

    async def get_today_stats(self) -> dict[str, Any]:
        """Quick stats for today only."""
        return await self.get_full_stats(days=1)

    # ── Inventory Monitor ──────────────────────────────────────────────

    async def scan_mrkt_inventory(self, owner_tg_id: int) -> list[dict[str, Any]]:
        """Scan MRKT account and detect changes (new gifts, sold gifts)."""
        if not self._mrkt:
            return []

        changes: list[dict[str, Any]] = []
        try:
            gifts = await self._mrkt.get_my_gifts(owner_tg_id, count=100)
        except Exception as e:
            logger.warning("MRKT inventory scan failed: %s", e)
            return []

        current: dict[str, dict[str, Any]] = {}
        for g in gifts:
            gid = g.get("id", "")
            if not gid:
                continue
            current[gid] = {
                "name": g.get("collectionName", "?"),
                "collection": g.get("collectionName", "?"),
                "price": g.get("salePrice", 0) or 0,
                "on_sale": g.get("isOnSale", False) or g.get("isOnAuction", False),
            }

        # Detect sold: was in known, not in current
        if self._known_mrkt_gifts:
            for gid, info in self._known_mrkt_gifts.items():
                if gid not in current:
                    price = info.get("price", 0)
                    if price > 0:
                        changes.append(
                            {
                                "action": "sell",
                                "market": "mrkt",
                                "gift_id": gid,
                                "name": info["name"],
                                "collection": info["collection"],
                                "price": price,
                            }
                        )

        # Detect new: in current, not in known
        if self._known_mrkt_gifts:
            for gid, info in current.items():
                if gid not in self._known_mrkt_gifts:
                    changes.append(
                        {
                            "action": "buy",
                            "market": "mrkt",
                            "gift_id": gid,
                            "name": info["name"],
                            "collection": info["collection"],
                            "price": info["price"],
                        }
                    )

        self._known_mrkt_gifts = current
        return changes

    async def scan_getgems_inventory(self) -> list[dict[str, Any]]:
        """Scan Getgems wallet and detect changes."""
        if not self._gg or not self._gg._wallet_address:
            return []

        changes: list[dict[str, Any]] = []
        try:
            gifts = await self._gg.get_user_offchain_gifts(self._gg._wallet_address)
        except Exception as e:
            logger.warning("Getgems inventory scan failed: %s", e)
            return []

        current: dict[str, dict[str, Any]] = {}
        for g in gifts:
            addr = g.get("address", "") or g.get("nft_address", "")
            if not addr:
                continue
            name = g.get("name", "") or g.get("collection", "?")
            price = g.get("price_nanoton", 0) or g.get("sale", {}).get("price", 0)
            current[addr] = {
                "name": name,
                "collection": name.split("#")[0].strip() if "#" in name else name,
                "price": price,
                "on_sale": bool(g.get("sale")),
            }

        if self._known_gg_gifts:
            for gid, info in self._known_gg_gifts.items():
                if gid not in current:
                    price = info.get("price", 0)
                    if price > 0:
                        changes.append(
                            {
                                "action": "sell",
                                "market": "getgems",
                                "gift_id": gid,
                                "name": info["name"],
                                "collection": info["collection"],
                                "price": price,
                            }
                        )

            for gid, info in current.items():
                if gid not in self._known_gg_gifts:
                    changes.append(
                        {
                            "action": "buy",
                            "market": "getgems",
                            "gift_id": gid,
                            "name": info["name"],
                            "collection": info["collection"],
                            "price": info["price"],
                        }
                    )

        self._known_gg_gifts = current
        return changes

    async def scan_tg_inventory(self, gift_transfer: Any = None) -> list[dict[str, Any]]:
        """Scan Telegram gifts and detect changes."""
        if not gift_transfer or not gift_transfer.ready:
            return []

        changes: list[dict[str, Any]] = []
        try:
            gifts = await gift_transfer.get_my_gifts(limit=50)
        except Exception as e:
            logger.warning("TG inventory scan failed: %s", e)
            return []

        current: dict[str, dict[str, Any]] = {}
        for g in gifts:
            gid = str(g.get("gift_id", ""))
            if not gid:
                continue
            name = g.get("title") or g.get("slug") or f"Gift #{gid}"
            current[gid] = {
                "name": name,
                "collection": name.split("#")[0].strip() if "#" in name else name,
                "is_unique": g.get("is_unique", False),
            }

        if self._known_tg_gifts:
            for gid, info in self._known_tg_gifts.items():
                if gid not in current and info.get("is_unique"):
                    # Gift left TG — likely transferred to MRKT/Getgems
                    changes.append(
                        {
                            "action": "transfer_out",
                            "market": "telegram",
                            "gift_id": gid,
                            "name": info["name"],
                            "collection": info["collection"],
                            "price": 0,
                        }
                    )

            for gid, info in current.items():
                if gid not in self._known_tg_gifts and info.get("is_unique"):
                    changes.append(
                        {
                            "action": "transfer_in",
                            "market": "telegram",
                            "gift_id": gid,
                            "name": info["name"],
                            "collection": info["collection"],
                            "price": 0,
                        }
                    )

        self._known_tg_gifts = current
        return changes

    async def run_inventory_scan(
        self,
        owner_tg_id: int,
        gift_transfer: Any = None,
    ) -> list[dict[str, Any]]:
        """Run full inventory scan across all markets. Returns detected changes."""
        all_changes: list[dict[str, Any]] = []

        mrkt_changes = await self.scan_mrkt_inventory(owner_tg_id)
        gg_changes = await self.scan_getgems_inventory()
        tg_changes = await self.scan_tg_inventory(gift_transfer)

        for change in mrkt_changes + gg_changes:
            if change["action"] in ("buy", "sell") and change["price"] > 0:
                await self.record_manual_trade(
                    gift_name=change["name"],
                    collection=change["collection"],
                    action=change["action"],
                    market=change["market"],
                    price_nanoton=change["price"],
                    gift_id=change.get("gift_id", ""),
                    note="auto-detected",
                )
                all_changes.append(change)

        all_changes.extend(tg_changes)
        return all_changes

    # ── Balance Collection ─────────────────────────────────────────────

    async def collect_balance(self) -> dict[str, int]:
        """Collect current balances from all sources and record snapshot."""
        wallet_balance = 0
        mrkt_balance = 0
        gifts_value = 0
        gifts_count = 0

        # TON wallet
        if self._gg and self._gg._wallet_address:
            try:
                wallet_balance = await self._gg.get_wallet_balance()
            except Exception:
                pass

        # MRKT balance
        if self._mrkt:
            try:
                bal = await self._mrkt.get_balance()
                if bal and bal.get("hard") is not None:
                    mrkt_balance = bal["hard"]
            except Exception:
                pass

        # Gift values from floors
        if self._mds:
            mrkt_floors = self._mds.get_all_mrkt_floors()
            gg_floors = self._mds.get_all_gg_floors()

            # MRKT gifts
            for info in self._known_mrkt_gifts.values():
                coll = info.get("collection", "")
                fl = mrkt_floors.get(coll, 0)
                if fl > 0:
                    gifts_value += int(fl * 0.95)
                    gifts_count += 1

            # Getgems gifts
            for info in self._known_gg_gifts.values():
                coll = info.get("collection", "")
                fl = 0
                for k, v in gg_floors.items():
                    if k.lower() == coll.lower() or coll.lower() in k.lower():
                        fl = v
                        break
                if fl > 0:
                    gifts_value += int(fl * 0.95)
                    gifts_count += 1

        await self.record_balance_snapshot(
            wallet_balance=wallet_balance,
            mrkt_balance=mrkt_balance,
            gifts_value=gifts_value,
            gifts_count=gifts_count,
        )

        return {
            "wallet": wallet_balance,
            "mrkt": mrkt_balance,
            "gifts": gifts_value,
            "gifts_count": gifts_count,
            "total": wallet_balance + mrkt_balance + gifts_value,
        }
