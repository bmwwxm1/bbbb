"""Real PnL model with capital efficiency and annualized ROI.

All money-related calculations here. Never scattered across modules.

Net profit accounts for:
  - Marketplace buy fees
  - Marketplace sell fees
  - Gas costs
  - Slippage buffer
  - Price decay estimate
  - Relisting risk
  - Opportunity cost of locked capital
"""

from __future__ import annotations

from bot.models.types import NANOTON, PnLResult

# Fee rates (Getgems gifts have NO sell commission)
MRKT_BUY_FEE_PCT = 0.0
MRKT_SELL_FEE_PCT = 5.0
GETGEMS_SELL_FEE_PCT = 0.0  # gifts: no commission on Getgems

# Risk buffers (gifts are fixed-price, highly liquid)
SLIPPAGE_BUFFER_PCT = 0.0  # fixed price, no slippage
RELISTING_RISK_PCT = 0.5  # minimal relisting risk
PRICE_DECAY_PER_DAY_PCT = 0.5  # 0.5% daily decay

# Cost constants
GAS_COST_NANOTON = int(0.05 * NANOTON)  # ~0.05 TON per operation
TRANSFER_GAS_NANOTON = int(0.02 * NANOTON)

# Withdrawal fees (fixed cost to move gift to another account for selling)
MRKT_WITHDRAW_NANOTON = int(0.2 * NANOTON)
GETGEMS_WITHDRAW_NANOTON = int(0.3 * NANOTON)
FRAGMENT_WITHDRAW_NANOTON = 0  # same account

# Capital efficiency
RISK_FREE_RATE_ANNUAL = 0.05  # 5% annual (opportunity cost baseline)


def calculate_pnl(
    buy_price: int,
    expected_sell_price: int,
    buy_market_fee_pct: float = MRKT_BUY_FEE_PCT,
    sell_market_fee_pct: float = GETGEMS_SELL_FEE_PCT,
    expected_hold_hours: float = 24.0,
    needs_transfer: bool = True,
    sell_market: str = "getgems",
) -> PnLResult:
    """Full PnL calculation with all costs and risk adjustments."""

    if buy_price <= 0 or expected_sell_price <= 0:
        return PnLResult(
            gross_profit=0,
            net_profit=0,
            roi_pct=0.0,
            annualized_roi_pct=0.0,
            capital_efficiency=0.0,
            total_cost=0,
            fees=0,
            gas=0,
            slippage_buffer=0,
            decay_estimate=0,
            relisting_risk=0,
            opportunity_cost=0,
            expected_hold_hours=expected_hold_hours,
        )

    # Withdrawal fee based on sell market (case-insensitive)
    _sm = sell_market.lower()
    if _sm == "mrkt":
        withdraw_fee = MRKT_WITHDRAW_NANOTON
    elif _sm == "getgems":
        withdraw_fee = GETGEMS_WITHDRAW_NANOTON
    elif _sm == "fragment":
        withdraw_fee = FRAGMENT_WITHDRAW_NANOTON
    else:
        withdraw_fee = GETGEMS_WITHDRAW_NANOTON

    # Buy side
    buy_fee = int(buy_price * buy_market_fee_pct / 100)
    total_buy_cost = buy_price + buy_fee + GAS_COST_NANOTON
    if needs_transfer:
        total_buy_cost += TRANSFER_GAS_NANOTON + withdraw_fee

    # Sell side
    sell_fee = int(expected_sell_price * sell_market_fee_pct / 100)
    sell_revenue = expected_sell_price - sell_fee - GAS_COST_NANOTON

    # Risk adjustments
    slippage = int(expected_sell_price * SLIPPAGE_BUFFER_PCT / 100)
    relisting_risk = int(expected_sell_price * RELISTING_RISK_PCT / 100)

    hold_days = max(expected_hold_hours / 24.0, 1.0 / 24.0)
    decay = int(expected_sell_price * PRICE_DECAY_PER_DAY_PCT / 100 * hold_days)

    opp_cost = opportunity_cost(total_buy_cost, expected_hold_hours)

    # Net
    gross_profit = sell_revenue - buy_price
    net_profit = sell_revenue - total_buy_cost - slippage - relisting_risk - decay - opp_cost

    roi_pct = (net_profit / total_buy_cost * 100) if total_buy_cost > 0 else 0.0
    ann_roi = annualized_roi(roi_pct, expected_hold_hours)

    cap_efficiency = 0.0
    if total_buy_cost > 0 and hold_days > 0:
        cap_efficiency = net_profit / (total_buy_cost * hold_days)

    return PnLResult(
        gross_profit=gross_profit,
        net_profit=net_profit,
        roi_pct=roi_pct,
        annualized_roi_pct=ann_roi,
        capital_efficiency=cap_efficiency,
        total_cost=total_buy_cost,
        fees=buy_fee + sell_fee,
        gas=GAS_COST_NANOTON * 2 + (TRANSFER_GAS_NANOTON if needs_transfer else 0),
        slippage_buffer=slippage,
        decay_estimate=decay,
        relisting_risk=relisting_risk,
        opportunity_cost=opp_cost,
        expected_hold_hours=expected_hold_hours,
    )


MAX_ANNUALIZED_ROI = 100_000.0  # cap to avoid overflow in scoring


def annualized_roi(roi_pct: float, hold_hours: float) -> float:
    """Normalize ROI to annual basis for cross-opportunity comparison."""
    if hold_hours <= 0:
        return min(roi_pct * 8760, MAX_ANNUALIZED_ROI)
    hold_years = hold_hours / 8760
    if hold_years >= 1:
        return roi_pct
    try:
        result = ((1 + roi_pct / 100) ** (1 / hold_years) - 1) * 100
        return min(result, MAX_ANNUALIZED_ROI)
    except (OverflowError, ValueError):
        return min(roi_pct * 8760, MAX_ANNUALIZED_ROI)


def opportunity_cost(
    capital_locked: int,
    hold_hours: float,
    risk_free_rate: float = RISK_FREE_RATE_ANNUAL,
) -> int:
    """Cost of having capital locked instead of earning yield."""
    hourly_rate = risk_free_rate / 8760
    return int(capital_locked * hourly_rate * max(hold_hours, 0))
