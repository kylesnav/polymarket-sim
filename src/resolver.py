"""Trade resolution using Polymarket's own resolution data.

For multi-outcome trades (with event_id), uses Polymarket's resolution
outcome directly. Falls back to NOAA observations for legacy binary trades.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from src.journal import Journal
from src.models import NOAAObservation, Trade

if TYPE_CHECKING:
    from src.noaa import NOAAClient
    from src.polymarket import PolymarketClient

logger = structlog.get_logger()


def resolve_trades(
    journal: Journal,
    polymarket: PolymarketClient,
    noaa: NOAAClient | None = None,
) -> dict[str, object]:
    """Resolve unresolved trades using Polymarket resolution data.

    For trades with event_id (multi-outcome): queries Polymarket's API
    for the event's resolution outcome.
    For legacy trades (no event_id): falls back to NOAA observations.

    Args:
        journal: Trade journal for retrieving trades and market metadata.
        polymarket: Polymarket client for resolution data.
        noaa: Optional NOAA client for legacy trade resolution.

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

    # Cache resolution data per event to avoid redundant API calls
    resolution_cache: dict[str, dict[str, Decimal]] = {}

    for trade in unresolved:
        if trade.event_id:
            # Multi-outcome trade: use Polymarket resolution
            result = _resolve_via_polymarket(
                trade, polymarket, resolution_cache,
            )
        elif noaa is not None:
            # Legacy binary trade: fall back to NOAA
            result = _resolve_via_noaa(trade, journal, noaa)
        else:
            logger.debug(
                "skipping_legacy_trade_no_noaa",
                trade_id=trade.trade_id,
            )
            skipped += 1
            continue

        if result is None:
            skipped += 1
            continue

        outcome, actual_pnl = result

        success = journal.update_trade_resolution(
            trade_id=trade.trade_id,
            outcome=outcome,
            actual_pnl=actual_pnl,
        )

        if success:
            logger.info(
                "trade_resolved",
                trade_id=trade.trade_id,
                event_id=trade.event_id or trade.market_id,
                outcome=outcome,
                actual_pnl=str(actual_pnl),
                source="polymarket" if trade.event_id else "noaa_legacy",
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
        skipped=skipped,
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


def _resolve_via_polymarket(
    trade: Trade,
    polymarket: PolymarketClient,
    cache: dict[str, dict[str, Decimal]],
) -> tuple[str, Decimal] | None:
    """Resolve a multi-outcome trade using Polymarket's resolution data.

    Args:
        trade: Trade with event_id and condition_id.
        polymarket: Polymarket API client.
        cache: Resolution data cache keyed by event_id.

    Returns:
        Tuple of (outcome, actual_pnl) or None if not yet resolved.
    """
    event_id = trade.event_id
    if not event_id:
        return None

    # Check cache first
    if event_id not in cache:
        try:
            resolution_data = polymarket.get_resolution_data(event_id)
            cache[event_id] = resolution_data
        except Exception as e:
            logger.warning(
                "resolution_data_fetch_failed",
                event_id=event_id,
                error=str(e),
            )
            return None

    resolution_data = cache[event_id]
    if not resolution_data:
        logger.debug("event_not_yet_resolved", event_id=event_id)
        return None

    # Look up this trade's token/condition in the resolution
    token_id = trade.token_id
    if not token_id:
        logger.warning(
            "trade_missing_token_id",
            trade_id=trade.trade_id,
            event_id=event_id,
        )
        return None

    # Resolution data is keyed by token_id: 1.0 for winner, 0.0 for losers
    final_price = resolution_data.get(token_id)
    if final_price is None:
        logger.warning(
            "token_not_in_resolution",
            trade_id=trade.trade_id,
            token_id=token_id,
        )
        return None

    # Determine outcome
    cost = trade.price if trade.side == "YES" else Decimal("1") - trade.price
    if final_price == Decimal("1"):
        # This bucket won
        if trade.side == "YES":
            outcome = "won"
            actual_pnl = trade.size * (Decimal("1") - cost) / cost
        else:
            outcome = "lost"
            actual_pnl = -trade.size
    else:
        # This bucket lost
        if trade.side == "YES":
            outcome = "lost"
            actual_pnl = -trade.size
        else:
            outcome = "won"
            actual_pnl = trade.size * (Decimal("1") - cost) / cost

    return outcome, actual_pnl


def _resolve_via_noaa(
    trade: Trade,
    journal: Journal,
    noaa: NOAAClient,
) -> tuple[str, Decimal] | None:
    """Resolve a legacy binary trade using NOAA observations.

    Args:
        trade: Legacy trade with market_id.
        journal: Journal for market metadata lookup.
        noaa: NOAA client for weather observations.

    Returns:
        Tuple of (outcome, actual_pnl) or None if cannot resolve.
    """
    market_data = journal.get_market_metadata(trade.market_id)
    if market_data is None:
        logger.warning(
            "market_metadata_not_found",
            market_id=trade.market_id,
            trade_id=trade.trade_id,
        )
        return None

    event_date = market_data["event_date"]
    if not isinstance(event_date, date):
        return None

    today = date.today()
    if event_date >= today:
        logger.info(
            "skipping_future_event",
            trade_id=trade.trade_id,
            event_date=str(event_date),
        )
        return None

    lat = float(str(market_data["lat"]))
    lon = float(str(market_data["lon"]))

    observation = noaa.get_observations(lat, lon, event_date)
    if observation is None:
        return None

    result = _calculate_outcome(
        trade=trade,
        observation=observation,
        metric=str(market_data["metric"]),
        threshold=float(market_data["threshold"]),  # type: ignore[arg-type]
        comparison=str(market_data["comparison"]),
    )

    if result.outcome is None or result.actual_pnl is None:
        return None

    return result.outcome, result.actual_pnl


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
        metric: Metric type.
        threshold: Threshold value for the event.
        comparison: Comparison type ("above", "below").

    Returns:
        _OutcomeResult with outcome, pnl, actual weather value, and unit.
    """
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

    condition_met = False
    if comparison == "above":
        condition_met = actual_value > threshold
    elif comparison == "below":
        condition_met = actual_value < threshold
    else:
        return _OutcomeResult(None, None, actual_value, unit)

    won = condition_met if trade.side == "YES" else not condition_met

    cost = trade.price if trade.side == "YES" else Decimal("1") - trade.price
    if won:
        outcome = "won"
        actual_pnl = trade.size * (Decimal("1") - cost) / cost
    else:
        outcome = "lost"
        actual_pnl = -trade.size

    return _OutcomeResult(outcome, actual_pnl, actual_value, unit)
