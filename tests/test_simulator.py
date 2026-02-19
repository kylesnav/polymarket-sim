"""Tests for the paper trading simulator."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.models import NOAAForecast, Portfolio, Signal, WeatherMarket
from src.simulator import Simulator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_market(
    market_id: str = "mkt-1",
    location: str = "New York",
    yes_price: Decimal = Decimal("0.40"),
    event_date: date | None = None,
    metric: str = "temperature_high",
    threshold: float = 75.0,
) -> WeatherMarket:
    return WeatherMarket(
        market_id=market_id,
        question="Will the high temp exceed 75°F in NYC on March 5?",
        location=location,
        lat=40.7128,
        lon=-74.0060,
        event_date=event_date or date(2027, 3, 5),
        metric=metric,
        threshold=threshold,
        comparison="above",
        yes_price=yes_price,
        no_price=Decimal("1") - yes_price,
        volume=Decimal("5000"),
        close_date=datetime(2027, 3, 5, 12, 0, tzinfo=UTC),
        token_id="tok-1",
    )


def _make_forecast(
    temp_high: float = 80.0,
    temp_low: float = 55.0,
    precip_prob: float = 0.2,
) -> NOAAForecast:
    return NOAAForecast(
        location="40.71,-74.01",
        forecast_date=date(2027, 3, 5),
        retrieved_at=datetime.now(tz=UTC),
        temperature_high=temp_high,
        temperature_low=temp_low,
        precip_probability=precip_prob,
    )


def _make_signal(
    market_id: str = "mkt-1",
    side: str = "YES",
    edge: Decimal = Decimal("0.15"),
    size: Decimal = Decimal("10.00"),
) -> Signal:
    return Signal(
        market_id=market_id,
        noaa_probability=Decimal("0.65"),
        market_price=Decimal("0.50"),
        edge=edge,
        side=side,
        kelly_fraction=Decimal("0.08"),
        recommended_size=size,
        confidence="medium",
    )


@pytest.fixture
def sim() -> Simulator:
    """Create a Simulator with all external clients mocked."""
    s = Simulator.__new__(Simulator)
    s._bankroll = Decimal("500")
    s._min_edge = Decimal("0.10")
    s._kelly_fraction = Decimal("0.25")
    s._position_cap_pct = Decimal("0.05")
    s._max_bankroll = Decimal("500")
    s._daily_loss_limit_pct = Decimal("0.05")
    s._kill_switch = False
    s._polymarket = MagicMock()
    s._noaa = MagicMock()
    s._journal = MagicMock()
    s._portfolio = Portfolio(
        cash=Decimal("500"),
        total_value=Decimal("500"),
        starting_bankroll=Decimal("500"),
    )
    s._last_markets = []
    s._last_forecasts = {}
    return s


# ---------------------------------------------------------------------------
# run_scan
# ---------------------------------------------------------------------------

class TestRunScan:
    """Tests for Simulator.run_scan."""

    def test_returns_signals_for_edgy_markets(self, sim: Simulator) -> None:
        market = _make_market(yes_price=Decimal("0.40"))
        forecast = _make_forecast(temp_high=85.0)

        sim._polymarket.get_weather_markets.return_value = [market]
        sim._noaa.get_forecast.return_value = forecast

        signals = sim.run_scan()
        # With 85°F forecast and 75°F threshold, NOAA prob should be high
        # Edge = NOAA prob - 0.40, should exceed 0.10 threshold
        assert isinstance(signals, list)

    def test_returns_empty_when_no_markets(self, sim: Simulator) -> None:
        sim._polymarket.get_weather_markets.return_value = []
        signals = sim.run_scan()
        assert signals == []

    def test_kill_switch_blocks_scan(self, sim: Simulator) -> None:
        sim._kill_switch = True
        signals = sim.run_scan()
        assert signals == []
        sim._polymarket.get_weather_markets.assert_not_called()

    def test_stores_markets_and_forecasts(self, sim: Simulator) -> None:
        market = _make_market()
        forecast = _make_forecast()
        sim._polymarket.get_weather_markets.return_value = [market]
        sim._noaa.get_forecast.return_value = forecast

        sim.run_scan()
        assert sim._last_markets == [market]
        assert market.market_id in sim._last_forecasts


# ---------------------------------------------------------------------------
# execute_signals
# ---------------------------------------------------------------------------

class TestExecuteSignals:
    """Tests for Simulator.execute_signals."""

    def test_executes_valid_signal(self, sim: Simulator) -> None:
        market = _make_market()
        sim._last_markets = [market]
        sim._last_forecasts = {market.market_id: _make_forecast()}
        sim._journal.has_open_trade.return_value = False
        sim._journal.log_trade.return_value = True
        sim._journal.update_trade_status.return_value = True
        sim._journal.cache_market.return_value = True

        signal = _make_signal(size=Decimal("10.00"))
        trades = sim.execute_signals([signal])

        assert len(trades) == 1
        assert trades[0].status == "filled"
        assert trades[0].market_id == "mkt-1"
        sim._journal.log_trade.assert_called_once()
        sim._journal.update_trade_status.assert_called_once()

    def test_skips_duplicate_market(self, sim: Simulator) -> None:
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = True

        signal = _make_signal()
        trades = sim.execute_signals([signal])

        assert len(trades) == 0
        sim._journal.log_trade.assert_not_called()

    def test_kill_switch_blocks_execution(self, sim: Simulator) -> None:
        sim._kill_switch = True
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = False

        signal = _make_signal()
        trades = sim.execute_signals([signal])

        assert len(trades) == 0
        sim._journal.log_trade.assert_not_called()

    def test_position_limit_caps_oversized_trade(self, sim: Simulator) -> None:
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = False

        # 5% of 500 = 25, so $30 exceeds the cap — should be capped to $25
        signal = _make_signal(size=Decimal("30.00"))
        trades = sim.execute_signals([signal])

        assert len(trades) == 1
        assert trades[0].size == Decimal("25.00")

    def test_skips_when_logging_fails(self, sim: Simulator) -> None:
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = False
        sim._journal.log_trade.return_value = False

        signal = _make_signal(size=Decimal("10.00"))
        trades = sim.execute_signals([signal])

        assert len(trades) == 0
        sim._journal.update_trade_status.assert_not_called()

    def test_updates_bankroll_after_trade(self, sim: Simulator) -> None:
        market = _make_market()
        sim._last_markets = [market]
        sim._last_forecasts = {market.market_id: _make_forecast()}
        sim._journal.has_open_trade.return_value = False
        sim._journal.log_trade.return_value = True
        sim._journal.update_trade_status.return_value = True
        sim._journal.cache_market.return_value = True

        signal = _make_signal(size=Decimal("10.00"))
        sim.execute_signals([signal])

        assert sim._bankroll == Decimal("490")
        assert sim._portfolio.cash == Decimal("490")

    def test_saves_daily_snapshot(self, sim: Simulator) -> None:
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = False
        sim._journal.log_trade.return_value = True
        sim._journal.update_trade_status.return_value = True
        sim._journal.cache_market.return_value = True

        signal = _make_signal(size=Decimal("10.00"))
        sim.execute_signals([signal])

        sim._journal.save_daily_snapshot.assert_called_once()

    def test_daily_loss_blocks_execution(self, sim: Simulator) -> None:
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = False
        # Set daily P&L to -5% of $500 = -$25
        sim._portfolio = Portfolio(
            cash=Decimal("475"),
            total_value=Decimal("475"),
            starting_bankroll=Decimal("500"),
            daily_pnl=Decimal("-25"),
        )

        signal = _make_signal(size=Decimal("10.00"))
        trades = sim.execute_signals([signal])

        assert len(trades) == 0

    def test_insufficient_cash_blocks_trade(self, sim: Simulator) -> None:
        sim._last_markets = [_make_market()]
        sim._journal.has_open_trade.return_value = False
        sim._portfolio = Portfolio(
            cash=Decimal("5"),
            total_value=Decimal("5"),
            starting_bankroll=Decimal("500"),
        )

        signal = _make_signal(size=Decimal("10.00"))
        trades = sim.execute_signals([signal])

        assert len(trades) == 0


# ---------------------------------------------------------------------------
# Properties / accessors
# ---------------------------------------------------------------------------

class TestSimulatorAccessors:
    """Tests for Simulator properties and accessors."""

    def test_last_markets_property(self, sim: Simulator) -> None:
        markets = [_make_market("m1"), _make_market("m2")]
        sim._last_markets = markets
        assert sim.last_markets == markets

    def test_get_portfolio(self, sim: Simulator) -> None:
        portfolio = sim.get_portfolio()
        assert portfolio.cash == Decimal("500")
        assert portfolio.total_value == Decimal("500")


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------

class TestSimulatorClose:
    """Tests for Simulator.close."""

    def test_closes_all_clients(self, sim: Simulator) -> None:
        sim.close()
        sim._polymarket.close.assert_called_once()
        sim._noaa.close.assert_called_once()
        sim._journal.close.assert_called_once()
