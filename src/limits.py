"""Hard limit checks for position sizing and risk management.

Each check returns (allowed, reason) where allowed=False means the trade
must be rejected.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

logger = structlog.get_logger()


def check_position_limit(
    trade_size: Decimal,
    bankroll: Decimal,
    cap_pct: Decimal = Decimal("0.05"),
) -> tuple[bool, str]:
    """Check that a single position does not exceed the per-position cap.

    Args:
        trade_size: Proposed trade size in dollars.
        bankroll: Current bankroll in dollars.
        cap_pct: Maximum fraction of bankroll per position. Default 5%.

    Returns:
        Tuple of (allowed, reason).
    """
    max_position = bankroll * cap_pct
    if trade_size > max_position:
        reason = (
            f"Trade size ${trade_size} exceeds position cap "
            f"${max_position} ({cap_pct:.0%} of ${bankroll})"
        )
        logger.warning("position_limit_exceeded", trade_size=trade_size, max_position=max_position)
        return False, reason
    return True, "OK"


def check_bankroll_limit(
    cash: Decimal,
    pending: Decimal,
    total_value: Decimal,
    max_bankroll: Decimal,
) -> tuple[bool, str]:
    """Check that there is sufficient cash and portfolio hasn't grown past ceiling.

    Buying a position converts cash to exposure — it doesn't increase total
    value.  The max_bankroll ceiling only prevents reinvesting gains that
    have pushed total_value above the cap.

    Args:
        cash: Available cash in dollars.
        pending: Pending trade size in dollars.
        total_value: Current total portfolio value in dollars.
        max_bankroll: Maximum allowed bankroll.

    Returns:
        Tuple of (allowed, reason).
    """
    if pending > cash:
        reason = (
            f"Insufficient cash: ${cash} available, "
            f"${pending} required"
        )
        logger.warning("insufficient_cash", cash=cash, pending=pending)
        return False, reason
    if total_value > max_bankroll:
        reason = (
            f"Portfolio value ${total_value} exceeds "
            f"max bankroll ${max_bankroll} — halt reinvestment of gains"
        )
        logger.warning(
            "bankroll_ceiling_exceeded",
            total_value=total_value,
            max_bankroll=max_bankroll,
        )
        return False, reason
    return True, "OK"


def check_daily_loss(
    daily_pnl: Decimal,
    starting_bankroll: Decimal,
    limit_pct: Decimal = Decimal("0.05"),
) -> tuple[bool, str]:
    """Check that daily losses have not exceeded the daily loss limit.

    Args:
        daily_pnl: Today's profit/loss in dollars (negative = loss).
        starting_bankroll: Bankroll at start of day.
        limit_pct: Maximum daily loss as fraction of starting bankroll. Default 5%.

    Returns:
        Tuple of (allowed, reason).
    """
    max_loss = starting_bankroll * limit_pct
    if daily_pnl < Decimal("0") and abs(daily_pnl) >= max_loss:
        reason = (
            f"Daily loss ${daily_pnl} exceeds limit "
            f"-${max_loss} ({limit_pct:.0%} of ${starting_bankroll})"
        )
        logger.warning("daily_loss_limit_hit", daily_pnl=daily_pnl, max_loss=max_loss)
        return False, reason
    return True, "OK"


def check_kill_switch(kill_switch: bool) -> tuple[bool, str]:
    """Check if the kill switch is engaged.

    Args:
        kill_switch: Whether the kill switch is active.

    Returns:
        Tuple of (allowed, reason).
    """
    if kill_switch:
        logger.warning("kill_switch_engaged")
        return False, "Kill switch is engaged — all trading halted"
    return True, "OK"
