"""Fee calculator for cross-market arbitrage.

Calculates net profit after marketplace fees AND withdrawal/transfer fees.

Withdrawal fees (fixed TON cost to move a gift between accounts):
  - MRKT:     0.2 TON
  - Getgems:  0.3 TON
  - Fragment: 0.0 TON (same account, no transfer)
  - Portal:   0.25 TON
"""

from bot.config import settings

NANO = 1_000_000_000


def _withdraw_fee_nano(market: str) -> int:
    """Withdrawal fee in nanoTON for a given sell market."""
    if market in ("mrkt", "MRKT"):
        return int(settings.mrkt_withdraw_fee_ton * NANO)
    if market in ("getgems", "Getgems", "GETGEMS"):
        return int(settings.getgems_withdraw_fee_ton * NANO)
    if market in ("fragment", "Fragment", "FRAGMENT"):
        return int(settings.fragment_withdraw_fee_ton * NANO)
    if market in ("portal", "Portal", "PORTAL"):
        return int(settings.portal_withdraw_fee_ton * NANO)
    return 0


def net_sell_price_mrkt(gross_price: int) -> int:
    """What you actually receive after MRKT sell fee."""
    fee = int(gross_price * settings.mrkt_sell_fee_pct / 100)
    return gross_price - fee


def net_sell_price_getgems(gross_price: int) -> int:
    """What you actually receive after Getgems sell fee."""
    fee = int(gross_price * settings.getgems_sell_fee_pct / 100)
    return gross_price - fee


def net_sell_price_portal(gross_price: int) -> int:
    """What you actually receive after Portal sell fee (currently 0%)."""
    fee = int(gross_price * settings.portal_sell_fee_pct / 100)
    return gross_price - fee


def net_sell_price(gross_price: int, market: str) -> int:
    """Net sell revenue for any market (after % fee)."""
    if market in ("mrkt", "MRKT"):
        return net_sell_price_mrkt(gross_price)
    if market in ("getgems", "Getgems", "GETGEMS"):
        return net_sell_price_getgems(gross_price)
    if market in ("portal", "Portal", "PORTAL"):
        return net_sell_price_portal(gross_price)
    # Fragment: no sell fee currently
    return gross_price


def total_buy_cost_mrkt(listing_price: int) -> int:
    """Total cost to buy on MRKT (price + buy fee)."""
    fee = int(listing_price * settings.mrkt_buy_fee_pct / 100)
    return listing_price + fee


def total_buy_cost(buy_price: int, buy_market: str) -> int:
    """Total cost to buy on any market (price + buy fee)."""
    if buy_market in ("mrkt", "MRKT"):
        return total_buy_cost_mrkt(buy_price)
    if buy_market in ("portal", "Portal", "PORTAL"):
        fee = int(buy_price * settings.portal_buy_fee_pct / 100)
        return buy_price + fee
    # Getgems, Fragment: no buy fee
    return buy_price


def cross_market_profit(
    buy_price: int,
    sell_price: int,
    buy_market: str = "mrkt",
    sell_market: str = "getgems",
) -> int:
    """Net profit: buy on one market → sell on another, including withdrawal fee."""
    cost = total_buy_cost(buy_price, buy_market)

    revenue = net_sell_price(sell_price, sell_market)
    withdraw = _withdraw_fee_nano(sell_market)
    return revenue - cost - withdraw


def cross_market_roi(
    buy_price: int,
    sell_price: int,
    buy_market: str = "mrkt",
    sell_market: str = "getgems",
) -> float:
    """ROI % for cross-market trade, including withdrawal fee."""
    cost = total_buy_cost(buy_price, buy_market)
    if cost <= 0:
        return 0.0
    profit = cross_market_profit(buy_price, sell_price, buy_market, sell_market)
    return (profit / cost) * 100


def within_mrkt_profit(buy_price: int, order_price: int) -> int:
    """Net profit: buy listing → sell into order on MRKT."""
    cost = total_buy_cost_mrkt(buy_price)
    return order_price - cost


def within_mrkt_roi(buy_price: int, order_price: int) -> float:
    """ROI % for within-MRKT arbitrage."""
    cost = total_buy_cost_mrkt(buy_price)
    if cost <= 0:
        return 0.0
    profit = within_mrkt_profit(buy_price, order_price)
    return (profit / cost) * 100


def min_getgems_sell_for_profit(
    buy_price_mrkt: int,
    min_roi_pct: float | None = None,
) -> int:
    """Minimum Getgems listing price to achieve min_roi_pct profit (including withdrawal)."""
    if min_roi_pct is None:
        min_roi_pct = settings.min_roi_percent
    cost = total_buy_cost_mrkt(buy_price_mrkt)
    withdraw = _withdraw_fee_nano("getgems")
    target_revenue = int((cost + withdraw) * (1 + min_roi_pct / 100))
    fee_multiplier = 1 - settings.getgems_sell_fee_pct / 100
    if fee_multiplier <= 0:
        return 0
    return int(target_revenue / fee_multiplier) + 1
