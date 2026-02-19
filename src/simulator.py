"""Paper trading engine for simulation mode.

Fetches markets, generates signals, and executes paper trades
with all safety rails enforced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from src.journal import Journal
from src.limits import (
    check_bankroll_limit,
    check_daily_loss,
    check_kill_switch,
    check_position_limit,
)
from src.models import NOAAForecast, Portfolio, Signal, Trade, WeatherMarket
from src.noaa import NOAAClient
from src.polymarket import PolymarketClient
from src.strategy import scan_weather_markets

logger = structlog.get_logger()


class Simulator:
    """Paper trading simulator.

    Orchestrates market scanning, signal generation, and simulated
    trade execution with full safety rail enforcement.
    """

    def __init__(
        self,
        bankroll: Decimal,
        min_edge: Decimal = Decimal("0.10"),
        kelly_fraction: Decimal = Decimal("0.25"),
        position_cap_pct: Decimal = Decimal("0.05"),
        max_bankroll: Decimal = Decimal("500"),
        daily_loss_limit_pct: Decimal = Decimal("0.05"),
        kill_switch: bool = False,
    ) -> None:
        """Initialize the simulator.

        Args:
            bankroll: Starting bankroll in dollars.
            min_edge: Minimum edge threshold for signals.
            kelly_fraction: Kelly multiplier for position sizing.
            position_cap_pct: Maximum position size as fraction of bankroll.
            max_bankroll: Maximum allowed bankroll.
            daily_loss_limit_pct: Daily loss halt threshold.
            kill_switch: Whether the kill switch is engaged.
        """
        self._bankroll = bankroll
        self._min_edge = min_edge
        self._kelly_fraction = kelly_fraction
        self._position_cap_pct = position_cap_pct
        self._max_bankroll = max_bankroll
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._kill_switch = kill_switch

        self._polymarket = PolymarketClient()
        self._noaa = NOAAClient()
        self._journal = Journal()

        self._portfolio = Portfolio(
            cash=bankroll,
            total_value=bankroll,
            starting_bankroll=bankroll,
        )

        self._last_markets: list[WeatherMarket] = []

        logger.info("simulator_initialized", bankroll=str(bankroll))

    def run_scan(self) -> list[Signal]:
        """Fetch markets, get forecasts, and generate trading signals.

        Returns:
            List of actionable trading signals.
        """
        # Check kill switch
        allowed, reason = check_kill_switch(self._kill_switch)
        if not allowed:
            logger.warning("scan_blocked", reason=reason)
            return []

        logger.info("starting_market_scan")

        # Fetch weather markets from Polymarket
        markets = self._polymarket.get_weather_markets()
        if not markets:
            logger.info("no_weather_markets_found")
            return []

        self._last_markets = markets
        logger.info("weather_markets_found", count=len(markets))

        # Fetch NOAA forecasts for each market
        forecasts = self._fetch_forecasts(markets)
        logger.info("forecasts_fetched", count=len(forecasts))

        # Generate signals
        signals = scan_weather_markets(
            markets=markets,
            forecasts=forecasts,
            min_edge=self._min_edge,
            kelly_fraction=self._kelly_fraction,
            bankroll=self._bankroll,
            position_cap_pct=self._position_cap_pct,
            max_bankroll=self._max_bankroll,
            daily_loss_limit_pct=self._daily_loss_limit_pct,
            kill_switch=self._kill_switch,
            portfolio=self._portfolio,
        )

        logger.info("signals_generated", count=len(signals))
        return signals

    def execute_signals(self, signals: list[Signal]) -> list[Trade]:
        """Execute paper trades for each signal, enforcing all safety rails.

        Follows log-before-execute pattern: trade intent is logged to the
        journal before the simulated fill is recorded.

        Args:
            signals: List of trading signals to execute.

        Returns:
            List of executed Trade records.
        """
        trades: list[Trade] = []

        # Build market lookup from last scan
        market_lookup: dict[str, WeatherMarket] = {}
        for market in self._last_markets:
            market_lookup[market.market_id] = market

        for signal in signals:
            # Check for existing open trade on this market
            if self._journal.has_open_trade(signal.market_id):
                logger.info(
                    "skipping_duplicate_market",
                    market_id=signal.market_id,
                )
                continue

            # Pre-execution limit checks
            allowed, reason = check_kill_switch(self._kill_switch)
            if not allowed:
                logger.warning("trade_blocked_kill_switch", market_id=signal.market_id)
                continue

            allowed, reason = check_daily_loss(
                self._portfolio.daily_pnl,
                self._portfolio.starting_bankroll,
                self._daily_loss_limit_pct,
            )
            if not allowed:
                logger.warning("trade_blocked_daily_loss", market_id=signal.market_id)
                continue

            allowed, reason = check_position_limit(
                signal.recommended_size,
                self._bankroll,
                self._position_cap_pct,
            )
            if not allowed:
                logger.warning(
                    "trade_blocked_position_limit",
                    market_id=signal.market_id,
                    reason=reason,
                )
                continue

            allowed, reason = check_bankroll_limit(
                cash=self._portfolio.cash,
                pending=signal.recommended_size,
                total_value=self._portfolio.total_value,
                max_bankroll=self._max_bankroll,
            )
            if not allowed:
                logger.warning(
                    "trade_blocked_bankroll_limit",
                    market_id=signal.market_id,
                    reason=reason,
                )
                continue

            # Create trade record
            trade = Trade(
                market_id=signal.market_id,
                side=signal.side,
                price=signal.market_price,
                size=signal.recommended_size,
                noaa_probability=signal.noaa_probability,
                edge=signal.edge,
                timestamp=datetime.now(tz=UTC),
                status="pending",
            )

            # LOG BEFORE EXECUTE — safety rail #7
            logged = self._journal.log_trade(trade)
            if not logged:
                logger.error(
                    "trade_logging_failed_skipping",
                    trade_id=trade.trade_id,
                )
                continue

            # Cache market metadata for resolution
            if signal.market_id in market_lookup:
                market = market_lookup[signal.market_id]
                self._journal.cache_market(
                    market_id=market.market_id,
                    location=market.location,
                    lat=market.lat,
                    lon=market.lon,
                    event_date=market.event_date,
                    metric=market.metric,
                    threshold=market.threshold,
                    comparison=market.comparison,
                )

            # Simulate the fill
            self._journal.update_trade_status(trade.trade_id, "filled")
            filled_trade = Trade(
                trade_id=trade.trade_id,
                market_id=trade.market_id,
                side=trade.side,
                price=trade.price,
                size=trade.size,
                noaa_probability=trade.noaa_probability,
                edge=trade.edge,
                timestamp=trade.timestamp,
                status="filled",
            )
            trades.append(filled_trade)

            # Update portfolio (simplified: deduct size from cash)
            new_cash = self._portfolio.cash - signal.recommended_size
            pnl_estimate = signal.edge * signal.recommended_size
            self._portfolio = Portfolio(
                cash=new_cash,
                total_value=new_cash,
                daily_pnl=self._portfolio.daily_pnl + pnl_estimate,
                starting_bankroll=self._portfolio.starting_bankroll,
            )

            logger.info(
                "paper_trade_executed",
                trade_id=trade.trade_id,
                market_id=trade.market_id,
                side=trade.side,
                size=str(trade.size),
                edge=str(trade.edge),
            )

        # Save daily snapshot
        today = date.today()
        self._journal.save_daily_snapshot(
            snapshot_date=today,
            cash=self._portfolio.cash,
            total_value=self._portfolio.total_value,
            daily_pnl=self._portfolio.daily_pnl,
            open_positions=len(self._portfolio.positions),
            trades_today=len(trades),
        )

        logger.info(
            "simulation_summary",
            trades_executed=len(trades),
            daily_pnl=str(self._portfolio.daily_pnl),
            bankroll=str(self._portfolio.total_value),
        )

        return trades

    def _fetch_forecasts(
        self, markets: list[WeatherMarket]
    ) -> dict[str, NOAAForecast]:
        """Fetch NOAA forecasts for a list of markets.

        Args:
            markets: Weather markets to fetch forecasts for.

        Returns:
            Dict mapping market_id to NOAAForecast.
        """
        forecasts: dict[str, NOAAForecast] = {}

        for market in markets:
            forecast = self._noaa.get_forecast(
                lat=market.lat,
                lon=market.lon,
                target_date=market.event_date,
            )
            if forecast is not None:
                forecasts[market.market_id] = forecast
                logger.debug(
                    "forecast_fetched",
                    market_id=market.market_id,
                    location=market.location,
                )
            else:
                logger.warning(
                    "forecast_unavailable",
                    market_id=market.market_id,
                    location=market.location,
                )

        return forecasts

    def get_portfolio(self) -> Portfolio:
        """Get the current portfolio state.

        Returns:
            Current Portfolio snapshot.
        """
        return self._portfolio

    def close(self) -> None:
        """Close all client connections."""
        self._polymarket.close()
        self._noaa.close()
        self._journal.close()
