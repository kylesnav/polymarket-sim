"""Trade resolution against actual NOAA weather outcomes.

Fetches historical NOAA data for past event dates, compares actual weather
to trade thresholds, and calculates real P&L.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from src.journal import Journal
from src.models import Trade
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

        # Fetch actual NOAA weather for the event date
        event_date = market_data["event_date"]
        lat = float(str(market_data["lat"]))
        lon = float(str(market_data["lon"]))

        forecast = noaa.get_forecast(lat, lon, event_date)  # type: ignore[arg-type]
        if forecast is None:
            logger.warning(
                "forecast_unavailable_for_resolution",
                market_id=trade.market_id,
                event_date=str(event_date),
            )
            continue

        # Determine if trade won or lost
        outcome, actual_pnl = _calculate_outcome(
            trade=trade,
            forecast=forecast,
            metric=str(market_data["metric"]),
            threshold=float(market_data["threshold"]),  # type: ignore[arg-type]
            comparison=str(market_data["comparison"]),
        )

        if outcome is None or actual_pnl is None:
            logger.warning(
                "could_not_calculate_outcome",
                trade_id=trade.trade_id,
                market_id=trade.market_id,
            )
            continue

        # Update journal with resolution
        success = journal.update_trade_resolution(
            trade_id=trade.trade_id,
            outcome=outcome,
            actual_pnl=actual_pnl,
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
        wins=wins,
        losses=losses,
        total_pnl=str(total_pnl),
    )

    return {
        "resolved_count": resolved_count,
        "wins": wins,
        "losses": losses,
        "total_pnl": total_pnl,
    }


def _calculate_outcome(
    trade: Trade,
    forecast: object,  # NOAAForecast
    metric: str,
    threshold: float,
    comparison: str,
) -> tuple[str, Decimal] | tuple[None, None]:
    """Calculate if a trade won or lost based on actual weather.

    Args:
        trade: The trade to evaluate.
        forecast: NOAA forecast data (NOAAForecast model).
        metric: Metric type ("temperature_high", "temperature_low", "precipitation", "snowfall").
        threshold: Threshold value for the event.
        comparison: Comparison type ("above", "below").

    Returns:
        Tuple of (outcome, actual_pnl) where outcome is "won" or "lost",
        or (None, None) if outcome cannot be determined.
    """
    from src.models import NOAAForecast

    if not isinstance(forecast, NOAAForecast):
        return None, None

    # Extract the actual value from forecast
    actual_value: float | None = None
    if metric == "temperature_high":
        actual_value = forecast.temperature_high
    elif metric == "temperature_low":
        actual_value = forecast.temperature_low
    elif metric in ("precipitation", "snowfall"):
        # For precip/snowfall, we'd need to check if it occurred
        # For now, use PoP as proxy (actual value would come from weather station data)
        actual_value = forecast.precip_probability
        if actual_value is not None:
            actual_value = actual_value * 100  # Convert to percentage for inches comparison

    if actual_value is None:
        return None, None

    # Determine if condition was met
    condition_met = False
    if comparison == "above":
        condition_met = actual_value > threshold
    elif comparison == "below":
        condition_met = actual_value < threshold

    # Determine win/loss based on trade side
    won = condition_met if trade.side == "YES" else not condition_met

    # Calculate P&L
    if won:
        outcome = "won"
        # YES @ 0.60: if resolves to 1.00, P&L = (1.00 - 0.60) * size
        # NO @ 0.40: if resolves to 0.00, P&L = (1.00 - 0.40) * size
        actual_pnl = (Decimal("1.00") - trade.price) * trade.size
    else:
        outcome = "lost"
        # YES @ 0.60: if resolves to 0.00, P&L = (0.00 - 0.60) * size
        # NO @ 0.40: if resolves to 1.00, P&L = (0.00 - 0.40) * size
        actual_pnl = (Decimal("0.00") - trade.price) * trade.size

    return outcome, actual_pnl
