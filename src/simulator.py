"""Paper trading engine for simulation mode.

Fetches markets, generates signals, and executes paper trades
with all safety rails enforced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from src.correlation import compute_correlated_exposure
from src.executor import PaperExecutor, SimulatedExecutor, TradeExecutor
from src.journal import Journal
from src.limits import (
    check_bankroll_limit,
    check_daily_loss,
    check_kill_switch,
)
from src.models import (
    BucketSignal,
    NOAAForecast,
    Portfolio,
    Signal,
    Trade,
    WeatherEvent,
    WeatherMarket,
)
from src.noaa import NOAAClient
from src.polymarket import PolymarketClient
from src.resolver import resolve_trades
from src.strategy import scan_weather_events, scan_weather_markets

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
        position_cap_pct: Decimal = Decimal("0.25"),
        max_bankroll: Decimal = Decimal("500"),
        daily_loss_limit_pct: Decimal = Decimal("0.05"),
        kill_switch: bool = False,
        min_volume: Decimal = Decimal("1000"),
        max_spread: Decimal = Decimal("0.05"),
        max_forecast_horizon_days: int = 5,
        max_forecast_age_hours: float = 12.0,
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
            min_volume: Minimum market volume to consider.
            max_spread: Maximum bid-ask spread to consider.
            max_forecast_horizon_days: Skip markets beyond this horizon.
            max_forecast_age_hours: Skip forecasts older than this.
        """
        self._bankroll = bankroll
        self._min_edge = min_edge
        self._kelly_fraction = kelly_fraction
        self._position_cap_pct = position_cap_pct
        self._max_bankroll = max_bankroll
        self._daily_loss_limit_pct = daily_loss_limit_pct
        self._kill_switch = kill_switch
        self._min_volume = min_volume
        self._max_spread = max_spread
        self._max_forecast_horizon_days = max_forecast_horizon_days
        self._max_forecast_age_hours = max_forecast_age_hours

        self._polymarket = PolymarketClient()
        self._noaa = NOAAClient()
        self._journal = Journal()
        self._executor: TradeExecutor = PaperExecutor(self._polymarket)
        self._legacy_executor: TradeExecutor = SimulatedExecutor()

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
        self._last_events: list[WeatherEvent] = []
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
        stats = resolve_trades(self._journal, self._polymarket, self._noaa)
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
            min_volume=self._min_volume,
            max_spread=self._max_spread,
            max_forecast_horizon_days=self._max_forecast_horizon_days,
            max_forecast_age_hours=self._max_forecast_age_hours,
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
            # Check existing exposure including correlated positions
            max_position = self._max_bankroll * self._position_cap_pct
            correlated_exposure = compute_correlated_exposure(
                signal, self._last_markets, self._journal.get_open_position_size,
            )
            remaining_room = max_position - correlated_exposure

            if remaining_room <= Decimal("0"):
                logger.info(
                    "skipping_position_full",
                    market_id=signal.market_id,
                    correlated_exposure=str(correlated_exposure),
                    cap=str(max_position),
                )
                self._last_skip_reasons.append({
                    "market_id": signal.market_id,
                    "reason": (
                        f"Position full: ${correlated_exposure} deployed "
                        f"(incl. correlated), cap is ${max_position}"
                    ),
                })
                continue

            existing_size = self._journal.get_open_position_size(signal.market_id)
            is_double_down = existing_size > Decimal("0")

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

            # Cap to remaining room under position limit
            if trade_size > remaining_room:
                logger.info(
                    "trade_size_capped",
                    market_id=signal.market_id,
                    original=str(trade_size),
                    capped=str(remaining_room),
                    existing=str(existing_size),
                    double_down=is_double_down,
                )
                trade_size = remaining_room

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

            # Create pending trade record for log-before-execute
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

            # Execute via executor (simulated or live), with rollback on failure
            try:
                executor_result = self._executor.execute(signal, trade_size)
                if executor_result is None:
                    logger.error("executor_fill_failed", trade_id=trade.trade_id)
                    self._journal.update_trade_status(trade.trade_id, "cancelled")
                    continue

                # Update journal with the fill — use the pending trade_id for continuity
                self._journal.update_trade_status(trade.trade_id, "filled")
                filled_trade = Trade(
                    trade_id=trade.trade_id,
                    market_id=executor_result.market_id,
                    side=executor_result.side,
                    price=executor_result.price,
                    size=executor_result.size,
                    noaa_probability=executor_result.noaa_probability,
                    edge=executor_result.edge,
                    timestamp=executor_result.timestamp,
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
            except Exception as e:
                logger.error(
                    "trade_execution_failed",
                    trade_id=trade.trade_id,
                    error=str(e),
                )
                self._journal.update_trade_status(trade.trade_id, "cancelled")
                self._last_skip_reasons.append({
                    "market_id": signal.market_id,
                    "reason": f"Execution failed: {e}",
                })
                continue

            logger.info(
                "paper_trade_executed",
                trade_id=trade.trade_id,
                market_id=trade.market_id,
                side=trade.side,
                size=str(trade.size),
                edge=str(trade.edge),
                double_down=is_double_down,
                total_position=str(existing_size + trade_size),
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

    def run_event_scan(self) -> list[BucketSignal]:
        """Fetch multi-outcome events, get forecasts, generate bucket signals.

        Uses the new multi-outcome pipeline: fetches WeatherEvents, computes
        NOAA probability distributions, and generates per-bucket signals.

        Returns:
            List of actionable bucket-level trading signals.
        """
        allowed, reason = check_kill_switch(self._kill_switch)
        if not allowed:
            logger.warning("event_scan_blocked", reason=reason)
            return []

        self.resolve_pending()
        logger.info("starting_event_scan")

        events = self._polymarket.get_weather_events()
        if not events:
            logger.info("no_weather_events_found")
            return []

        today = date.today()
        active_events = [e for e in events if e.event_date >= today]
        self._last_events = active_events
        logger.info("weather_events_found", count=len(active_events))

        forecasts = self._fetch_event_forecasts(active_events)
        self._last_forecasts = forecasts
        logger.info("event_forecasts_fetched", count=len(forecasts))

        signals = scan_weather_events(
            events=active_events,
            forecasts=forecasts,
            min_edge=self._min_edge,
            kelly_fraction=self._kelly_fraction,
            bankroll=self._bankroll,
            position_cap_pct=self._position_cap_pct,
            max_bankroll=self._max_bankroll,
            daily_loss_limit_pct=self._daily_loss_limit_pct,
            kill_switch=self._kill_switch,
            portfolio=self._portfolio,
            max_forecast_horizon_days=self._max_forecast_horizon_days,
        )

        logger.info("bucket_signals_generated", count=len(signals))
        return signals

    def execute_bucket_signals(self, signals: list[BucketSignal]) -> list[Trade]:
        """Execute paper trades for bucket-level signals.

        Args:
            signals: List of bucket-level trading signals.

        Returns:
            List of executed Trade records.
        """
        trades: list[Trade] = []
        self._last_skip_reasons = []

        # Build event lookup
        event_lookup: dict[str, WeatherEvent] = {
            e.event_id: e for e in self._last_events
        }

        for signal in signals:
            allowed, reason = check_kill_switch(self._kill_switch)
            if not allowed:
                self._last_skip_reasons.append({
                    "market_id": signal.event_id, "reason": "Kill switch engaged",
                })
                continue

            allowed, reason = check_daily_loss(
                self._portfolio.daily_pnl,
                self._portfolio.starting_bankroll,
                self._daily_loss_limit_pct,
            )
            if not allowed:
                self._last_skip_reasons.append({
                    "market_id": signal.event_id, "reason": "Daily loss limit",
                })
                continue

            trade_size = signal.recommended_size

            allowed, reason = check_bankroll_limit(
                cash=self._portfolio.cash,
                pending=trade_size,
                total_value=self._portfolio.total_value,
                max_bankroll=self._max_bankroll,
            )
            if not allowed:
                self._last_skip_reasons.append({
                    "market_id": signal.event_id, "reason": reason,
                })
                continue

            # Create pending trade for log-before-execute
            trade = Trade(
                market_id="",
                side=signal.side,
                price=signal.market_price,
                size=trade_size,
                noaa_probability=signal.noaa_probability,
                edge=signal.edge,
                timestamp=datetime.now(tz=UTC),
                status="pending",
                event_id=signal.event_id,
                bucket_index=signal.bucket_index,
                token_id=signal.token_id,
                outcome_label=signal.outcome_label,
            )

            # LOG BEFORE EXECUTE
            event = event_lookup.get(signal.event_id)
            context: dict[str, object] | None = None
            if event:
                context = {
                    "question": event.question,
                    "location": event.location,
                    "event_date": event.event_date.isoformat(),
                    "metric": event.metric,
                    "threshold": 0,
                    "comparison": "",
                }
                forecast = self._last_forecasts.get(signal.event_id)
                if forecast:
                    context["noaa_forecast_high"] = forecast.temperature_high
                    context["noaa_forecast_low"] = forecast.temperature_low
                    context["noaa_forecast_narrative"] = forecast.forecast_narrative
                # Cache event for resolution
                self._journal.cache_event(event)

            logged = self._journal.log_trade(trade, market_context=context)
            if not logged:
                self._last_skip_reasons.append({
                    "market_id": signal.event_id,
                    "reason": "Trade logging failed",
                })
                continue

            try:
                executor_result = self._executor.execute(signal, trade_size)
                if executor_result is None:
                    self._journal.update_trade_status(trade.trade_id, "cancelled")
                    continue

                self._journal.update_trade_status(trade.trade_id, "filled")
                filled_trade = Trade(
                    trade_id=trade.trade_id,
                    market_id=executor_result.market_id,
                    side=executor_result.side,
                    price=executor_result.price,
                    size=executor_result.size,
                    noaa_probability=executor_result.noaa_probability,
                    edge=executor_result.edge,
                    timestamp=executor_result.timestamp,
                    status="filled",
                    event_id=signal.event_id,
                    bucket_index=signal.bucket_index,
                    token_id=signal.token_id,
                    outcome_label=signal.outcome_label,
                    fill_price=executor_result.fill_price,
                    book_depth_at_signal=executor_result.book_depth_at_signal,
                )
                trades.append(filled_trade)

                new_cash = self._portfolio.cash - trade_size
                self._portfolio = Portfolio(
                    cash=new_cash,
                    total_value=self._portfolio.total_value,
                    starting_bankroll=self._portfolio.starting_bankroll,
                )
                self._bankroll = new_cash
            except Exception as e:
                logger.error(
                    "bucket_trade_execution_failed",
                    trade_id=trade.trade_id,
                    error=str(e),
                )
                self._journal.update_trade_status(trade.trade_id, "cancelled")
                continue

            logger.info(
                "bucket_trade_executed",
                trade_id=trade.trade_id,
                event_id=signal.event_id,
                bucket=signal.outcome_label,
                side=trade.side,
                size=str(trade.size),
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

        return trades

    def _fetch_event_forecasts(
        self, events: list[WeatherEvent]
    ) -> dict[str, NOAAForecast]:
        """Fetch NOAA forecasts for a list of events.

        Args:
            events: Weather events to fetch forecasts for.

        Returns:
            Dict mapping event_id to NOAAForecast.
        """
        if not events:
            return {}

        requests = [
            (e.event_id, e.lat, e.lon, e.event_date) for e in events
        ]
        return self._noaa.batch_get_forecasts(requests, max_workers=10)

    def _fetch_forecasts(
        self, markets: list[WeatherMarket]
    ) -> dict[str, NOAAForecast]:
        """Fetch NOAA forecasts for a list of markets using parallel fetching.

        Args:
            markets: Weather markets to fetch forecasts for.

        Returns:
            Dict mapping market_id to NOAAForecast.
        """
        if not markets:
            return {}

        requests = [
            (m.market_id, m.lat, m.lon, m.event_date) for m in markets
        ]
        return self._noaa.batch_get_forecasts(requests, max_workers=10)

    @property
    def last_events(self) -> list[WeatherEvent]:
        """Get events from the most recent event scan.

        Returns:
            List of WeatherEvent objects from the last event scan.
        """
        return self._last_events

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
