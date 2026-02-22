"""Core strategy: compare NOAA forecasts against Polymarket prices.

Converts NOAA point forecasts into probabilities and generates trading
signals when the edge exceeds the configured threshold.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from src.limits import (
    check_bankroll_limit,
    check_daily_loss,
    check_kill_switch,
    check_position_limit,
)
from src.models import NBMPercentiles, NOAAForecast, Portfolio, Signal, WeatherMarket
from src.rules import evaluate_extreme_value
from src.sizing import calculate_kelly

logger = structlog.get_logger()

# Fallback NOAA forecast error standard deviations (Fahrenheit)
# Used only when NBM percentile data is unavailable
_FALLBACK_TEMP_STD_DEV_1DAY: float = 3.0
_FALLBACK_TEMP_STD_DEV_2DAY: float = 4.0
_FALLBACK_TEMP_STD_DEV_DEFAULT: float = 5.0

# Forecast horizon confidence multipliers
# Scales NOAA probability toward 0.5 (uncertainty) for distant forecasts
_HORIZON_MULTIPLIERS: dict[int, float] = {
    0: 1.0,
    1: 1.0,
    2: 0.85,
    3: 0.70,
    4: 0.55,
    5: 0.55,
}
_HORIZON_MULTIPLIER_DISTANT: float = 0.40


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
    *,
    min_volume: Decimal = Decimal("0"),
    max_spread: Decimal = Decimal("1"),
    max_forecast_horizon_days: int = 7,
    max_forecast_age_hours: float = 12.0,
    nbm_data: dict[str, NBMPercentiles] | None = None,
    enable_extreme_value_rules: bool = True,
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
        min_volume: Minimum market volume to consider.
        max_spread: Maximum bid-ask spread to consider.
        max_forecast_horizon_days: Skip markets beyond this horizon.
        max_forecast_age_hours: Skip forecasts older than this.
        nbm_data: Optional NBM percentile data keyed by market_id.
        enable_extreme_value_rules: Whether to evaluate extreme value rules.

    Returns:
        List of trading signals sorted by forecast horizon (shortest first).
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
    today = date.today()
    now = datetime.now(tz=UTC)

    for market in markets:
        # Volume filter
        if market.volume < min_volume:
            logger.debug(
                "market_filtered_low_volume",
                market_id=market.market_id,
                volume=str(market.volume),
                min_volume=str(min_volume),
            )
            continue

        # Spread filter: yes + no prices should sum to ~1.0
        spread = Decimal("1") - (market.yes_price + market.no_price)
        if abs(spread) > max_spread:
            logger.debug(
                "market_filtered_wide_spread",
                market_id=market.market_id,
                spread=str(spread),
                max_spread=str(max_spread),
            )
            continue

        # Forecast horizon filter
        days_out = max(0, (market.event_date - today).days)
        if days_out > max_forecast_horizon_days:
            logger.debug(
                "market_filtered_horizon",
                market_id=market.market_id,
                days_out=days_out,
                max_days=max_forecast_horizon_days,
            )
            continue

        forecast = forecasts.get(market.market_id)
        if forecast is None:
            logger.debug("no_forecast_for_market", market_id=market.market_id)
            continue

        # Forecast freshness check
        if forecast.update_time is not None:
            forecast_age_hours = (now - forecast.update_time).total_seconds() / 3600
            if forecast_age_hours > max_forecast_age_hours:
                logger.warning(
                    "forecast_too_stale",
                    market_id=market.market_id,
                    age_hours=round(forecast_age_hours, 1),
                    max_hours=max_forecast_age_hours,
                )
                continue

        # Get NBM percentile data if available
        nbm = nbm_data.get(market.market_id) if nbm_data else None

        noaa_prob = _noaa_to_probability(forecast, market, nbm=nbm)
        if noaa_prob is None:
            logger.debug("could_not_compute_probability", market_id=market.market_id)
            continue

        # Apply horizon confidence adjustment
        horizon_multiplier = _HORIZON_MULTIPLIERS.get(days_out, _HORIZON_MULTIPLIER_DISTANT)
        adjusted_prob = 0.5 + horizon_multiplier * (noaa_prob - 0.5)

        # Apply stale forecast penalty (6-12 hours old)
        if forecast.update_time is not None:
            forecast_age_hours = (now - forecast.update_time).total_seconds() / 3600
            if forecast_age_hours > 6.0:
                stale_factor = 0.5
                adjusted_prob = 0.5 + stale_factor * (adjusted_prob - 0.5)
                logger.info(
                    "forecast_stale_penalty",
                    market_id=market.market_id,
                    age_hours=round(forecast_age_hours, 1),
                )

        noaa_decimal = Decimal(str(adjusted_prob))
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

        # Check position limit (cap relative to max bankroll, not current cash)
        allowed, reason = check_position_limit(
            recommended_size, max_bankroll, position_cap_pct,
        )
        if not allowed:
            logger.info("position_limit_hit", market_id=market.market_id, reason=reason)
            # Cap to position limit
            recommended_size = max_bankroll * position_cap_pct

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

        # Market freshness boost: new markets (< 48h) are more likely mispriced
        if market.created_at is not None:
            market_age_hours = (now - market.created_at).total_seconds() / 3600
            if market_age_hours < 24 and abs_edge >= Decimal("0.10"):
                confidence = "high"
                logger.info(
                    "freshness_boost",
                    market_id=market.market_id,
                    age_hours=round(market_age_hours, 1),
                    original_confidence=confidence,
                )
            elif market_age_hours < 48 and confidence == "low":
                confidence = "medium"

        signal = Signal(
            market_id=market.market_id,
            noaa_probability=noaa_decimal,
            market_price=market.yes_price,
            edge=edge,
            side=side,  # type: ignore[arg-type]
            kelly_fraction=kelly_frac,
            recommended_size=recommended_size,
            confidence=confidence,  # type: ignore[arg-type]
            forecast_horizon_days=days_out,
        )
        signals.append(signal)
        logger.info(
            "signal_generated",
            market_id=market.market_id,
            side=side,
            edge=str(edge),
            size=str(recommended_size),
            horizon_days=days_out,
        )

    # Extreme value rules: evaluate markets that didn't produce a standard signal
    if enable_extreme_value_rules:
        signaled_ids = {s.market_id for s in signals}
        for market in markets:
            if market.market_id in signaled_ids:
                continue
            forecast = forecasts.get(market.market_id)
            if forecast is None:
                continue
            nbm = nbm_data.get(market.market_id) if nbm_data else None
            noaa_prob = _noaa_to_probability(forecast, market, nbm=nbm)
            if noaa_prob is None:
                continue
            noaa_decimal = Decimal(str(noaa_prob))
            ev_signal = evaluate_extreme_value(
                market=market,
                noaa_probability=noaa_decimal,
                bankroll=bankroll,
                min_edge=min_edge,
            )
            if ev_signal is not None:
                signals.append(ev_signal)

    # Sort by forecast horizon (shortest first) for cash allocation priority
    signals.sort(key=lambda s: s.forecast_horizon_days)

    logger.info("scan_complete", signals_found=len(signals), markets_scanned=len(markets))
    return signals


def compute_noaa_probability(
    forecast: NOAAForecast,
    market: WeatherMarket,
    *,
    nbm: NBMPercentiles | None = None,
) -> float | None:
    """Public wrapper for NOAA-to-probability conversion.

    Args:
        forecast: NOAA forecast data.
        market: Weather market with metric and threshold.
        nbm: Optional NBM percentile data for improved accuracy.

    Returns:
        Probability estimate (0-1) or None if insufficient data.
    """
    return _noaa_to_probability(forecast, market, nbm=nbm)


def _noaa_to_probability(
    forecast: NOAAForecast,
    market: WeatherMarket,
    *,
    nbm: NBMPercentiles | None = None,
) -> float | None:
    """Convert a NOAA forecast into a probability estimate for a market.

    For temperature markets: use NBM percentiles when available, otherwise
    model as normal distribution around the NOAA point forecast.
    For precipitation markets: use NOAA probability of precipitation directly.

    Args:
        forecast: NOAA forecast data.
        market: Weather market with metric and threshold.
        nbm: Optional NBM percentile data for temperature markets.

    Returns:
        Probability estimate (0-1) or None if insufficient data.
    """
    if market.metric == "precipitation":
        return _precip_probability(forecast, market)
    if market.metric == "snowfall":
        # TODO(V1): Implement proper snowfall probability model using NOAA QPF
        # and snow-to-liquid ratio. PoP is not a valid proxy for snowfall amount.
        logger.debug("snowfall_not_supported", market_id=getattr(market, "market_id", ""))
        return None
    if market.metric in ("temperature_high", "temperature_low"):
        return _temperature_probability(forecast, market, nbm=nbm)
    return None


def _temperature_probability(
    forecast: NOAAForecast,
    market: WeatherMarket,
    *,
    nbm: NBMPercentiles | None = None,
) -> float | None:
    """Compute probability of temperature exceeding/falling below threshold.

    Uses NBM percentile data when available for more accurate probability
    estimation. Falls back to normal distribution with hardcoded std devs.

    Args:
        forecast: NOAA forecast data.
        market: Weather market with threshold and comparison.
        nbm: Optional NBM percentile data.

    Returns:
        Probability (0-1) or None if insufficient data.
    """
    # Try NBM percentile interpolation first
    if nbm is not None:
        prob = _interpolate_nbm_probability(nbm, market.threshold)
        if prob is not None:
            if market.comparison == "above":
                return prob
            if market.comparison == "below":
                return 1.0 - prob
            return None

    # Fallback: normal distribution with point forecast
    if market.metric == "temperature_high":
        point_forecast = forecast.temperature_high
    else:
        point_forecast = forecast.temperature_low

    if point_forecast is None:
        return None

    # Choose std dev based on forecast horizon
    days_out = max(0, (market.event_date - date.today()).days)

    # Use NBM std_dev if available, otherwise fallback
    if nbm is not None and nbm.std_dev is not None and nbm.std_dev > 0:
        std_dev = nbm.std_dev
    elif days_out <= 1:
        std_dev = _FALLBACK_TEMP_STD_DEV_1DAY
    elif days_out <= 2:
        std_dev = _FALLBACK_TEMP_STD_DEV_2DAY
    else:
        std_dev = _FALLBACK_TEMP_STD_DEV_DEFAULT

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


def _interpolate_nbm_probability(
    nbm: NBMPercentiles, threshold: float
) -> float | None:
    """Interpolate NBM percentiles to estimate P(X > threshold).

    Uses linear interpolation between the 5 percentile points
    (p10, p25, p50, p75, p90) to estimate the probability of
    exceeding the threshold.

    Args:
        nbm: NBM percentile data.
        threshold: Temperature threshold to evaluate.

    Returns:
        P(X > threshold) or None if insufficient percentile data.
    """
    # Build percentile-value pairs from available data
    points: list[tuple[float, float]] = []
    for pct, val in [
        (0.10, nbm.p10),
        (0.25, nbm.p25),
        (0.50, nbm.p50),
        (0.75, nbm.p75),
        (0.90, nbm.p90),
    ]:
        if val is not None:
            points.append((pct, val))

    if len(points) < 2:
        return None

    # If threshold is below the lowest percentile value, P(X > threshold) is high
    if threshold <= points[0][1]:
        # Extrapolate: threshold is at or below p10, so P(X > threshold) >= 0.90
        return 1.0 - points[0][0] * (threshold / points[0][1]) if points[0][1] != 0 else 0.95

    # If threshold is above the highest percentile value, P(X > threshold) is low
    if threshold >= points[-1][1]:
        # Extrapolate: threshold is at or above p90, so P(X > threshold) <= 0.10
        return (1.0 - points[-1][0]) * (points[-1][1] / threshold) if threshold != 0 else 0.05

    # Linear interpolation between adjacent percentile points
    for i in range(len(points) - 1):
        pct_low, val_low = points[i]
        pct_high, val_high = points[i + 1]

        if val_low <= threshold <= val_high:
            if val_high == val_low:
                # Flat region: use midpoint percentile
                cdf_at_threshold = (pct_low + pct_high) / 2
            else:
                # Linear interpolation
                fraction = (threshold - val_low) / (val_high - val_low)
                cdf_at_threshold = pct_low + fraction * (pct_high - pct_low)

            return 1.0 - cdf_at_threshold

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
