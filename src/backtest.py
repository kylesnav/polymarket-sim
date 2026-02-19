"""Backtest engine: replay algorithm against resolved Polymarket weather markets.

Fetches recently closed weather markets, retrieves historical prices and
actual NOAA weather observations, then simulates what the algorithm would
have done. Uses actual observations as a proxy for NOAA forecasts (optimistic
upper bound since real forecasts have error margins).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog

from src.models import BacktestResult, BacktestTrade, NOAAForecast, WeatherMarket
from src.noaa import NOAAClient
from src.polymarket import PolymarketClient
from src.sizing import calculate_kelly
from src.strategy import compute_noaa_probability

logger = structlog.get_logger()


class Backtester:
    """Replay the trading algorithm against historical market data.

    Fetches resolved weather markets, samples historical prices,
    and compares against actual weather observations to measure
    algorithm performance.
    """

    def __init__(
        self,
        bankroll: Decimal = Decimal("500"),
        min_edge: Decimal = Decimal("0.10"),
        kelly_fraction: Decimal = Decimal("0.25"),
        position_cap_pct: Decimal = Decimal("0.05"),
        lookback_days: int = 7,
        price_offset_days: int = 2,
    ) -> None:
        """Initialize the backtester.

        Args:
            bankroll: Simulated bankroll in dollars.
            min_edge: Minimum edge threshold for signals.
            kelly_fraction: Kelly multiplier for position sizing.
            position_cap_pct: Maximum position size as fraction of bankroll.
            lookback_days: Days back to search for resolved markets.
            price_offset_days: Days before event to sample historical price.
        """
        self._bankroll = bankroll
        self._min_edge = min_edge
        self._kelly_fraction = kelly_fraction
        self._position_cap_pct = position_cap_pct
        self._lookback_days = lookback_days
        self._price_offset_days = price_offset_days

        self._polymarket = PolymarketClient()
        self._noaa = NOAAClient()

        logger.info(
            "backtester_initialized",
            bankroll=str(bankroll),
            lookback_days=lookback_days,
            price_offset_days=price_offset_days,
        )

    def run(self) -> BacktestResult:
        """Run the backtest against resolved weather markets.

        Returns:
            BacktestResult with individual trades and aggregate stats.
        """
        # 1. Fetch resolved weather markets
        markets = self._polymarket.get_resolved_weather_markets(
            lookback_days=self._lookback_days
        )
        logger.info("backtest_markets_fetched", count=len(markets))

        if not markets:
            logger.info("no_resolved_markets_found")
            return BacktestResult(markets_scanned=0)

        trades: list[BacktestTrade] = []
        wins = 0
        losses = 0
        total_pnl = Decimal("0")
        skipped = 0

        for market in markets:
            result = self._process_market(market)
            if result is None:
                skipped += 1
                continue

            trades.append(result)
            total_pnl += result.actual_pnl
            if result.outcome == "won":
                wins += 1
            else:
                losses += 1

        logger.info(
            "backtest_complete",
            markets_scanned=len(markets),
            trades=len(trades),
            skipped=skipped,
            wins=wins,
            losses=losses,
            total_pnl=str(total_pnl),
        )

        return BacktestResult(
            trades=trades,
            wins=wins,
            losses=losses,
            total_pnl=total_pnl,
            markets_scanned=len(markets),
            markets_skipped=skipped,
        )

    def _process_market(self, market: WeatherMarket) -> BacktestTrade | None:
        """Process a single resolved market for backtesting.

        Args:
            market: Resolved weather market to evaluate.

        Returns:
            BacktestTrade if the algorithm would have traded, None otherwise.
        """
        # a. Get historical price
        historical_price = self._get_historical_price(market)
        if historical_price is None:
            logger.info(
                "backtest_skip_no_price",
                market_id=market.market_id,
                location=market.location,
            )
            return None

        # b. Get actual weather observations
        observation = self._noaa.get_observations(
            lat=market.lat,
            lon=market.lon,
            target_date=market.event_date,
        )
        if observation is None:
            logger.info(
                "backtest_skip_no_observation",
                market_id=market.market_id,
                location=market.location,
            )
            return None

        # c. Determine actual outcome
        actual_value = _get_actual_value(observation, market.metric)
        if actual_value is None:
            logger.info(
                "backtest_skip_no_actual_value",
                market_id=market.market_id,
                metric=market.metric,
            )
            return None

        condition_met = _check_condition(actual_value, market.threshold, market.comparison)

        # d. Convert observation to forecast proxy
        forecast_proxy = _observation_to_forecast_proxy(observation)

        # e. Compute probability using the strategy
        market_with_historical_price = WeatherMarket(
            market_id=market.market_id,
            question=market.question,
            location=market.location,
            lat=market.lat,
            lon=market.lon,
            event_date=market.event_date,
            metric=market.metric,
            threshold=market.threshold,
            comparison=market.comparison,
            yes_price=historical_price,
            no_price=Decimal("1") - historical_price,
            volume=market.volume,
            close_date=market.close_date,
            token_id=market.token_id,
        )

        noaa_prob = compute_noaa_probability(forecast_proxy, market_with_historical_price)
        if noaa_prob is None:
            logger.info(
                "backtest_skip_no_probability",
                market_id=market.market_id,
            )
            return None

        noaa_decimal = Decimal(str(noaa_prob))
        edge = noaa_decimal - historical_price

        # Determine side
        if edge > Decimal("0") and edge >= self._min_edge:
            side = "YES"
        elif edge < Decimal("0") and abs(edge) >= self._min_edge:
            side = "NO"
        else:
            logger.debug(
                "backtest_edge_below_threshold",
                market_id=market.market_id,
                edge=str(edge),
            )
            return None

        # f. Calculate Kelly sizing
        kelly_frac, recommended_size = calculate_kelly(
            noaa_probability=noaa_decimal,
            market_price=historical_price,
            bankroll=self._bankroll,
            kelly_multiplier=self._kelly_fraction,
            min_edge=self._min_edge,
        )

        if recommended_size <= Decimal("0"):
            return None

        # Cap to position limit
        max_position = self._bankroll * self._position_cap_pct
        if recommended_size > max_position:
            recommended_size = max_position

        # Determine win/loss
        won = condition_met if side == "YES" else not condition_met

        if won:
            outcome = "won"
            actual_pnl = (Decimal("1.00") - historical_price) * recommended_size
        else:
            outcome = "lost"
            actual_pnl = (Decimal("0.00") - historical_price) * recommended_size

        trade = BacktestTrade(
            market_id=market.market_id,
            question=market.question,
            location=market.location,
            event_date=market.event_date,
            metric=market.metric,
            threshold=market.threshold,
            comparison=market.comparison,
            historical_price=historical_price,
            noaa_probability=noaa_decimal,
            edge=edge,
            side=side,  # type: ignore[arg-type]
            kelly_fraction=kelly_frac,
            size=recommended_size,
            actual_value=actual_value,
            condition_met=condition_met,
            outcome=outcome,  # type: ignore[arg-type]
            actual_pnl=actual_pnl,
        )

        logger.info(
            "backtest_trade",
            market_id=market.market_id,
            side=side,
            edge=str(edge),
            outcome=outcome,
            pnl=str(actual_pnl),
        )

        return trade

    def _get_historical_price(self, market: WeatherMarket) -> Decimal | None:
        """Get the market price from N days before the event.

        Args:
            market: Weather market to look up.

        Returns:
            Historical YES price or None if unavailable.
        """
        if not market.token_id:
            logger.debug("backtest_no_token_id", market_id=market.market_id)
            return None

        # Sample price from price_offset_days before event
        sample_date = market.event_date - timedelta(days=self._price_offset_days)
        start_ts = int(
            datetime(sample_date.year, sample_date.month, sample_date.day, tzinfo=UTC).timestamp()
        )
        end_ts = start_ts + 86400  # 24 hours

        prices = self._polymarket.get_price_history(
            token_id=market.token_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )

        if not prices:
            # Try a wider window
            start_ts -= 86400
            end_ts += 86400
            prices = self._polymarket.get_price_history(
                token_id=market.token_id,
                start_ts=start_ts,
                end_ts=end_ts,
            )

        if not prices:
            return None

        # Use the midpoint price from the sampled window
        mid_idx = len(prices) // 2
        return prices[mid_idx][1]

    def close(self) -> None:
        """Close all client connections."""
        self._polymarket.close()
        self._noaa.close()


def _observation_to_forecast_proxy(
    observation: NOAAForecast | object,
) -> NOAAForecast:
    """Convert a NOAA observation into a forecast-shaped proxy.

    This allows reusing the strategy's probability computation.
    The proxy treats actual observations as if they were forecasts,
    which makes the backtest optimistic (real forecasts have error).

    Args:
        observation: NOAAObservation with actual weather data.

    Returns:
        NOAAForecast populated from actual observations.
    """
    from src.models import NOAAObservation

    if not isinstance(observation, NOAAObservation):
        msg = f"Expected NOAAObservation, got {type(observation)}"
        raise TypeError(msg)

    return NOAAForecast(
        location=observation.location,
        forecast_date=observation.observation_date,
        retrieved_at=observation.retrieved_at,
        temperature_high=observation.temperature_high,
        temperature_low=observation.temperature_low,
        precip_probability=1.0 if (observation.precipitation or 0) > 0.01 else 0.0,
        forecast_narrative="[backtest proxy from actual observations]",
    )


def _get_actual_value(observation: object, metric: str) -> float | None:
    """Extract the relevant actual value from an observation.

    Args:
        observation: NOAAObservation with actual weather data.
        metric: Market metric type.

    Returns:
        Actual weather value or None.
    """
    from src.models import NOAAObservation

    if not isinstance(observation, NOAAObservation):
        return None

    if metric == "temperature_high":
        return observation.temperature_high
    if metric == "temperature_low":
        return observation.temperature_low
    if metric in ("precipitation", "snowfall"):
        return observation.precipitation
    return None


def _check_condition(actual_value: float, threshold: float, comparison: str) -> bool:
    """Check if the weather condition was met.

    Args:
        actual_value: Actual observed weather value.
        threshold: Market threshold.
        comparison: "above" or "below".

    Returns:
        True if the condition was met.
    """
    if comparison == "above":
        return actual_value > threshold
    if comparison == "below":
        return actual_value < threshold
    return False
