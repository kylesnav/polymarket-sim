"""Correlated position detection for weather markets.

Detects when multiple markets bet on the same weather event
(same location + metric + date) and applies combined position caps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable
    from decimal import Decimal

    from src.models import Signal, WeatherMarket

logger = structlog.get_logger()


def get_correlation_key(market: WeatherMarket) -> str:
    """Generate a correlation key for a market.

    Markets with the same key are correlated and should share
    a combined position cap.

    Args:
        market: Weather market to generate key for.

    Returns:
        Correlation key string (location|metric|date).
    """
    return f"{market.location.lower()}|{market.metric}|{market.event_date.isoformat()}"


def find_correlated_markets(
    signal: Signal,
    markets: list[WeatherMarket],
) -> list[str]:
    """Find market IDs correlated with a given signal.

    Two markets are correlated when they share the same location,
    metric type, and event date (regardless of threshold).

    Args:
        signal: The signal to check correlation for.
        markets: All known markets to check against.

    Returns:
        List of correlated market IDs (excluding the signal's own market).
    """
    target_market: WeatherMarket | None = None
    for m in markets:
        if m.market_id == signal.market_id:
            target_market = m
            break

    if target_market is None:
        return []

    target_key = get_correlation_key(target_market)
    correlated: list[str] = []

    for m in markets:
        if m.market_id == signal.market_id:
            continue
        if get_correlation_key(m) == target_key:
            correlated.append(m.market_id)

    if correlated:
        logger.info(
            "correlated_markets_found",
            signal_market=signal.market_id,
            correlated=correlated,
        )

    return correlated


def compute_correlated_exposure(
    signal: Signal,
    markets: list[WeatherMarket],
    get_position_size: Callable[[str], Decimal],
) -> Decimal:
    """Compute total exposure across correlated positions.

    Args:
        signal: The signal being evaluated.
        markets: All known markets.
        get_position_size: Function(market_id) -> Decimal returning open size.

    Returns:
        Total exposure across the signal's market and all correlated markets.
    """
    correlated_ids = find_correlated_markets(signal, markets)
    total = get_position_size(signal.market_id)

    for market_id in correlated_ids:
        total += get_position_size(market_id)

    return total
