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
from src.resolver import resolve_trades
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

        # Restore portfolio state from journal (accounts for existing trades)
        summary = self._journal.get_portfolio_summary(bankroll)
        restored_cash = Decimal(str(summary["cash"]))
        restored_total = Decimal(str(summary["total_value"]))
        self._portfolio = Portfolio(
            cash=restored_cash,
            total_value=restored_total,
            starting_bankroll=bankroll,
        )
        self._bankroll = restored_cash

        self._last_markets: list[WeatherMarket] = []
        self._last_forecasts: dict[str, NOAAForecast] = {}
        self._last_skip_reasons: list[dict[str, str]] = []

        logger.info(
            "simulator_initialized",
            starting_bankroll=str(bankroll),
            restored_cash=str(restored_cash),
            restored_total=str(restored_total),
        )

    def resolve_pending(self) -> dict[str, object]:
        """Resolve any trades whose event dates have passed.

        Fetches actual NOAA observations and calculates real P&L,
        then refreshes portfolio state from the journal.

        Returns:
            Resolution statistics dict.
        """
        stats = resolve_trades(self._journal, self._noaa)
        resolved_count = stats.get("resolved_count", 0)
        if resolved_count:
            logger.info("auto_resolved_trades", count=resolved_count)
            self._refresh_portfolio()
        return stats

    def _refresh_portfolio(self) -> None:
        """Refresh portfolio state from the journal after resolution."""
        summary = self._journal.get_portfolio_summary(
            self._portfolio.starting_bankroll
        )
        restored_cash = Decimal(str(summary["cash"]))
        restored_total = Decimal(str(summary["total_value"]))
        self._portfolio = Portfolio(
            cash=restored_cash,
            total_value=restored_total,
            starting_bankroll=self._portfolio.starting_bankroll,
        )
        self._bankroll = restored_cash

    def run_scan(self) -> list[Signal]:
        """Fetch markets, get forecasts, and generate trading signals.

        Auto-resolves past trades first, then filters out markets
        whose event dates have already passed.

        Returns:
            List of actionable trading signals.
        """
        # Check kill switch
        allowed, reason = check_kill_switch(self._kill_switch)
        if not allowed:
            logger.warning("scan_blocked", reason=reason)
            return []

        # Auto-resolve past trades to free up cash
        self.resolve_pending()

        logger.info("starting_market_scan")

        # Fetch weather markets from Polymarket
        markets = self._polymarket.get_weather_markets()
        if not markets:
            logger.info("no_weather_markets_found")
            return []

        # Filter out markets whose event dates have already passed
        today = date.today()
        active_markets = [m for m in markets if m.event_date >= today]
        filtered_count = len(markets) - len(active_markets)
        if filtered_count:
            logger.info("filtered_past_markets", count=filtered_count)

        self._last_markets = active_markets
        logger.info("weather_markets_found", count=len(active_markets))

        # Fetch NOAA forecasts for each market
        forecasts = self._fetch_forecasts(active_markets)
        self._last_forecasts = forecasts
        logger.info("forecasts_fetched", count=len(forecasts))

        # Generate signals
        signals = scan_weather_markets(
            markets=active_markets,
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
        self._last_skip_reasons = []

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
                self._last_skip_reasons.append({
                    "market_id": signal.market_id,
                    "reason": "Already have an open bet on this market",
                })
                continue

            # Pre-execution limit checks
            allowed, reason = check_kill_switch(self._kill_switch)
            if not allowed:
                logger.warning("trade_blocked_kill_switch", market_id=signal.market_id)
                self._last_skip_reasons.append({
                    "market_id": signal.market_id, "reason": "Kill switch engaged",
                })
                continue

            allowed, reason = check_daily_loss(
                self._portfolio.daily_pnl,
                self._portfolio.starting_bankroll,
                self._daily_loss_limit_pct,
            )
            if not allowed:
                logger.warning("trade_blocked_daily_loss", market_id=signal.market_id)
                self._last_skip_reasons.append({
                    "market_id": signal.market_id, "reason": "Daily loss limit reached",
                })
                continue

            trade_size = signal.recommended_size
            allowed, reason = check_position_limit(
                trade_size,
                self._max_bankroll,
                self._position_cap_pct,
            )
            if not allowed:
                # Cap to position limit instead of rejecting
                trade_size = self._max_bankroll * self._position_cap_pct
                logger.info(
                    "trade_size_capped",
                    market_id=signal.market_id,
                    original=str(signal.recommended_size),
                    capped=str(trade_size),
                )

            allowed, reason = check_bankroll_limit(
                cash=self._portfolio.cash,
                pending=trade_size,
                total_value=self._portfolio.total_value,
                max_bankroll=self._max_bankroll,
            )
            if not allowed:
                logger.warning(
                    "trade_blocked_bankroll_limit",
                    market_id=signal.market_id,
                    reason=reason,
                )
                self._last_skip_reasons.append({
                    "market_id": signal.market_id, "reason": reason,
                })
                continue

            # Create trade record
            trade = Trade(
                market_id=signal.market_id,
                side=signal.side,
                price=signal.market_price,
                size=trade_size,
                noaa_probability=signal.noaa_probability,
                edge=signal.edge,
                timestamp=datetime.now(tz=UTC),
                status="pending",
            )

            # LOG BEFORE EXECUTE — safety rail #7
            market = market_lookup.get(signal.market_id)
            context: dict[str, object] | None = None
            if market:
                context = {
                    "question": market.question,
                    "location": market.location,
                    "event_date": market.event_date.isoformat(),
                    "metric": market.metric,
                    "threshold": market.threshold,
                    "comparison": market.comparison,
                }
                forecast = self._last_forecasts.get(signal.market_id)
                if forecast:
                    context["noaa_forecast_high"] = forecast.temperature_high
                    context["noaa_forecast_low"] = forecast.temperature_low
                    context["noaa_forecast_narrative"] = forecast.forecast_narrative
            logged = self._journal.log_trade(trade, market_context=context)
            if not logged:
                logger.error(
                    "trade_logging_failed_skipping",
                    trade_id=trade.trade_id,
                )
                self._last_skip_reasons.append({
                    "market_id": signal.market_id,
                    "reason": "Trade logging failed (safety rail #7)",
                })
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

            # Update portfolio: subtract cash spent, total_value stays same (cash→exposure)
            new_cash = self._portfolio.cash - trade_size
            self._portfolio = Portfolio(
                cash=new_cash,
                total_value=self._portfolio.total_value,
                starting_bankroll=self._portfolio.starting_bankroll,
            )
            # Keep bankroll in sync with cash for accurate Kelly sizing
            self._bankroll = new_cash

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

    @property
    def last_markets(self) -> list[WeatherMarket]:
        """Get markets from the most recent scan.

        Returns:
            List of WeatherMarket objects from the last scan.
        """
        return self._last_markets

    @property
    def last_skip_reasons(self) -> list[dict[str, str]]:
        """Get skip reasons from the most recent execute_signals call.

        Returns:
            List of dicts with market_id and reason for each skipped signal.
        """
        return self._last_skip_reasons

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
