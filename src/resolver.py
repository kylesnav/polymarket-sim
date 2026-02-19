"""Trade resolution against actual NOAA weather outcomes.

Fetches historical NOAA data for past event dates, compares actual weather
to trade thresholds, and calculates real P&L.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import structlog

from src.journal import Journal
from src.models import NOAAObservation, Trade
from src.noaa import NOAAClient

logger = structlog.get_logger()


def resolve_trades(journal: Journal, noaa: NOAAClient) -> dict[str, object]:
    """Resolve unresolved trades against actual NOAA weather data.

    For each unresolved trade, fetches the actual weather for the event date,
    compares to the market threshold, and calculates real P&L.

    Args:
        journal: Trade journal for retrieving trades and market metadata.
        noaa: NOAA client for fetching historical weather data.

    Returns:
        Dict with resolution statistics (count, wins, losses, total_pnl).
    """
    unresolved = journal.get_unresolved_trades()
    if not unresolved:
        logger.info("no_unresolved_trades")
        return {
            "resolved_count": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": Decimal("0"),
        }

    resolved_count = 0
    wins = 0
    losses = 0
    total_pnl = Decimal("0")

    skipped = 0

    for trade in unresolved:
        # Get market metadata
        market_data = journal.get_market_metadata(trade.market_id)
        if market_data is None:
            logger.warning(
                "market_metadata_not_found",
                market_id=trade.market_id,
                trade_id=trade.trade_id,
            )
            continue

        event_date = market_data["event_date"]
        if not isinstance(event_date, date):
            logger.warning(
                "invalid_event_date",
                market_id=trade.market_id,
                event_date=str(event_date),
            )
            continue

        # Skip trades whose event date hasn't passed yet
        today = date.today()
        if event_date >= today:
            logger.info(
                "skipping_future_event",
                market_id=trade.market_id,
                trade_id=trade.trade_id,
                event_date=str(event_date),
            )
            skipped += 1
            continue

        # Fetch actual observed weather from NOAA stations
        lat = float(str(market_data["lat"]))
        lon = float(str(market_data["lon"]))

        observation = noaa.get_observations(lat, lon, event_date)
        if observation is None:
            logger.warning(
                "observations_unavailable_for_resolution",
                market_id=trade.market_id,
                event_date=str(event_date),
            )
            continue

        # Determine if trade won or lost
        result = _calculate_outcome(
            trade=trade,
            observation=observation,
            metric=str(market_data["metric"]),
            threshold=float(market_data["threshold"]),  # type: ignore[arg-type]
            comparison=str(market_data["comparison"]),
        )

        if result.outcome is None or result.actual_pnl is None:
            logger.warning(
                "could_not_calculate_outcome",
                trade_id=trade.trade_id,
                market_id=trade.market_id,
            )
            continue

        outcome = result.outcome
        actual_pnl = result.actual_pnl

        # Update journal with resolution
        success = journal.update_trade_resolution(
            trade_id=trade.trade_id,
            outcome=outcome,
            actual_pnl=actual_pnl,
            actual_value=result.actual_value,
            actual_value_unit=result.actual_value_unit,
        )

        if success:
            logger.info(
                "trade_resolved",
                trade_id=trade.trade_id,
                market_id=trade.market_id,
                outcome=outcome,
                actual_pnl=str(actual_pnl),
            )
            resolved_count += 1
            total_pnl += actual_pnl
            if outcome == "won":
                wins += 1
            else:
                losses += 1

    logger.info(
        "resolution_complete",
        resolved_count=resolved_count,
        skipped_future=skipped,
        wins=wins,
        losses=losses,
        total_pnl=str(total_pnl),
    )

    return {
        "resolved_count": resolved_count,
        "skipped_future": skipped,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
    }


class _OutcomeResult:
    """Result of trade outcome calculation."""

    __slots__ = ("outcome", "actual_pnl", "actual_value", "actual_value_unit")

    def __init__(
        self,
        outcome: str | None,
        actual_pnl: Decimal | None,
        actual_value: float | None,
        actual_value_unit: str,
    ) -> None:
        self.outcome = outcome
        self.actual_pnl = actual_pnl
        self.actual_value = actual_value
        self.actual_value_unit = actual_value_unit


def _calculate_outcome(
    trade: Trade,
    observation: NOAAObservation,
    metric: str,
    threshold: float,
    comparison: str,
) -> _OutcomeResult:
    """Calculate if a trade won or lost based on actual observed weather.

    Args:
        trade: The trade to evaluate.
        observation: Actual NOAA weather station observation data.
        metric: Metric type ("temperature_high", "temperature_low", "precipitation", "snowfall").
        threshold: Threshold value for the event.
        comparison: Comparison type ("above", "below").

    Returns:
        _OutcomeResult with outcome, pnl, actual weather value, and unit.
    """
    # Extract the actual value from observation
    actual_value: float | None = None
    unit = ""
    if metric in ("temperature_high", "temperature_low"):
        actual_value = (
            observation.temperature_high
            if metric == "temperature_high"
            else observation.temperature_low
        )
        unit = "\u00b0F"
    elif metric in ("precipitation", "snowfall"):
        actual_value = observation.precipitation
        unit = "in"

    if actual_value is None:
        return _OutcomeResult(None, None, None, "")

    # Determine if condition was met
    condition_met = False
    if comparison == "above":
        condition_met = actual_value > threshold
    elif comparison == "below":
        condition_met = actual_value < threshold
    else:
        # Unsupported comparison type (e.g. "between")
        return _OutcomeResult(None, None, actual_value, unit)

    # Determine win/loss based on trade side
    won = condition_met if trade.side == "YES" else not condition_met

    # Calculate P&L
    # trade.price is always the YES price. For NO trades, actual cost = 1 - yes_price.
    cost = trade.price if trade.side == "YES" else Decimal("1") - trade.price
    if won:
        outcome = "won"
        actual_pnl = (Decimal("1") - cost) * trade.size
    else:
        outcome = "lost"
        actual_pnl = -cost * trade.size

    return _OutcomeResult(outcome, actual_pnl, actual_value, unit)
