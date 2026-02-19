"""Core strategy: compare NOAA forecasts against Polymarket prices.

Converts NOAA point forecasts into probabilities and generates trading
signals when the edge exceeds the configured threshold.
"""

from __future__ import annotations

import math
from decimal import Decimal

import structlog

from src.limits import (
    check_bankroll_limit,
    check_daily_loss,
    check_kill_switch,
    check_position_limit,
)
from src.models import NOAAForecast, Portfolio, Signal, WeatherMarket
from src.sizing import calculate_kelly

logger = structlog.get_logger()

# Typical NOAA forecast error standard deviations (Fahrenheit)
# These are conservative estimates for 1-2 day forecasts
TEMP_STD_DEV_1DAY: float = 3.0
TEMP_STD_DEV_2DAY: float = 4.0
TEMP_STD_DEV_DEFAULT: float = 5.0


def scan_weather_markets(
    markets: list[WeatherMarket],
    forecasts: dict[str, NOAAForecast],
    min_edge: Decimal,
    kelly_fraction: Decimal,
    bankroll: Decimal,
    position_cap_pct: Decimal,
    max_bankroll: Decimal,
    daily_loss_limit_pct: Decimal,
    kill_switch: bool,
    portfolio: Portfolio,
) -> list[Signal]:
    """Compare NOAA forecasts against market prices and generate signals.

    Args:
        markets: List of weather markets to analyze.
        forecasts: Dict mapping market_id to NOAA forecast data.
        min_edge: Minimum edge threshold to generate a signal.
        kelly_fraction: Kelly multiplier (e.g., 0.25 for quarter-Kelly).
        bankroll: Current bankroll in dollars.
        position_cap_pct: Maximum position size as fraction of bankroll.
        max_bankroll: Maximum allowed bankroll.
        daily_loss_limit_pct: Daily loss halt threshold.
        kill_switch: Whether the kill switch is engaged.
        portfolio: Current portfolio state.

    Returns:
        List of trading signals for markets where edge exceeds threshold.
    """
    # Check kill switch first
    allowed, reason = check_kill_switch(kill_switch)
    if not allowed:
        logger.warning("scanning_halted", reason=reason)
        return []

    # Check daily loss
    allowed, reason = check_daily_loss(
        portfolio.daily_pnl,
        portfolio.starting_bankroll,
        daily_loss_limit_pct,
    )
    if not allowed:
        logger.warning("scanning_halted_daily_loss", reason=reason)
        return []

    signals: list[Signal] = []

    for market in markets:
        forecast = forecasts.get(market.market_id)
        if forecast is None:
            logger.debug("no_forecast_for_market", market_id=market.market_id)
            continue

        noaa_prob = _noaa_to_probability(forecast, market)
        if noaa_prob is None:
            logger.debug("could_not_compute_probability", market_id=market.market_id)
            continue

        noaa_decimal = Decimal(str(noaa_prob))
        edge = noaa_decimal - market.yes_price

        # Determine side
        if edge > Decimal("0") and edge >= min_edge:
            side: str = "YES"
        elif edge < Decimal("0") and abs(edge) >= min_edge:
            side = "NO"
        else:
            logger.debug(
                "edge_below_threshold",
                market_id=market.market_id,
                edge=edge,
                threshold=min_edge,
            )
            continue

        # Calculate Kelly sizing
        kelly_frac, recommended_size = calculate_kelly(
            noaa_probability=noaa_decimal,
            market_price=market.yes_price,
            bankroll=bankroll,
            kelly_multiplier=kelly_fraction,
            min_edge=min_edge,
        )

        if recommended_size <= Decimal("0"):
            continue

        # Check position limit
        allowed, reason = check_position_limit(
            recommended_size, bankroll, position_cap_pct,
        )
        if not allowed:
            logger.info("position_limit_hit", market_id=market.market_id, reason=reason)
            # Cap to position limit
            recommended_size = bankroll * position_cap_pct

        # Check bankroll limit: sufficient cash and portfolio not above ceiling
        allowed, reason = check_bankroll_limit(
            cash=portfolio.cash,
            pending=recommended_size,
            total_value=portfolio.total_value,
            max_bankroll=max_bankroll,
        )
        if not allowed:
            logger.info("bankroll_limit_hit", market_id=market.market_id, reason=reason)
            continue

        # Determine confidence
        abs_edge = abs(edge)
        if abs_edge >= Decimal("0.20"):
            confidence: str = "high"
        elif abs_edge >= Decimal("0.15"):
            confidence = "medium"
        else:
            confidence = "low"

        signal = Signal(
            market_id=market.market_id,
            noaa_probability=noaa_decimal,
            market_price=market.yes_price,
            edge=edge,
            side=side,  # type: ignore[arg-type]
            kelly_fraction=kelly_frac,
            recommended_size=recommended_size,
            confidence=confidence,  # type: ignore[arg-type]
        )
        signals.append(signal)
        logger.info(
            "signal_generated",
            market_id=market.market_id,
            side=side,
            edge=str(edge),
            size=str(recommended_size),
        )

    logger.info("scan_complete", signals_found=len(signals), markets_scanned=len(markets))
    return signals


def compute_noaa_probability(forecast: NOAAForecast, market: WeatherMarket) -> float | None:
    """Public wrapper for NOAA-to-probability conversion.

    Args:
        forecast: NOAA forecast data.
        market: Weather market with metric and threshold.

    Returns:
        Probability estimate (0-1) or None if insufficient data.
    """
    return _noaa_to_probability(forecast, market)


def _noaa_to_probability(forecast: NOAAForecast, market: WeatherMarket) -> float | None:
    """Convert a NOAA forecast into a probability estimate for a market.

    For temperature markets: model as normal distribution around the NOAA
    point forecast with a typical error margin.
    For precipitation markets: use NOAA probability of precipitation directly.

    Args:
        forecast: NOAA forecast data.
        market: Weather market with metric and threshold.

    Returns:
        Probability estimate (0-1) or None if insufficient data.
    """
    if market.metric == "precipitation":
        return _precip_probability(forecast, market)
    if market.metric == "snowfall":
        return _precip_probability(forecast, market)
    if market.metric in ("temperature_high", "temperature_low"):
        return _temperature_probability(forecast, market)
    return None


def _temperature_probability(forecast: NOAAForecast, market: WeatherMarket) -> float | None:
    """Compute probability of temperature exceeding/falling below threshold.

    Uses a normal distribution centered on the NOAA point forecast with
    a standard deviation based on typical forecast error.

    Args:
        forecast: NOAA forecast data.
        market: Weather market with threshold and comparison.

    Returns:
        Probability (0-1) or None if insufficient data.
    """
    if market.metric == "temperature_high":
        point_forecast = forecast.temperature_high
    else:
        point_forecast = forecast.temperature_low

    if point_forecast is None:
        return None

    # Choose std dev based on forecast horizon
    from datetime import date as date_cls

    days_out = (market.event_date - date_cls.today()).days
    if days_out <= 1:
        std_dev = TEMP_STD_DEV_1DAY
    elif days_out <= 2:
        std_dev = TEMP_STD_DEV_2DAY
    else:
        std_dev = TEMP_STD_DEV_DEFAULT

    if std_dev <= 0:
        return None

    # Z-score: how many std devs is the threshold from the forecast
    z = (market.threshold - point_forecast) / std_dev

    # P(X > threshold) using normal CDF complement
    prob_above = 1.0 - _normal_cdf(z)

    if market.comparison == "above":
        return prob_above
    if market.comparison == "below":
        return 1.0 - prob_above
    # "between" not fully supported — return None
    return None


def _precip_probability(forecast: NOAAForecast, market: WeatherMarket) -> float | None:
    """Use NOAA precipitation probability directly.

    Args:
        forecast: NOAA forecast data.
        market: Weather market (used for comparison direction).

    Returns:
        Probability (0-1) or None if insufficient data.
    """
    if forecast.precip_probability is None:
        return None

    pop = forecast.precip_probability  # Already 0-1

    if market.comparison == "above":
        return pop
    if market.comparison == "below":
        return 1.0 - pop
    return None


def _normal_cdf(z: float) -> float:
    """Standard normal cumulative distribution function.

    Args:
        z: Z-score value.

    Returns:
        P(Z <= z) for the standard normal distribution.
    """
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
