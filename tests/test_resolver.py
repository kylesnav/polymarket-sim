"""Tests for the resolver module."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from src.journal import Journal
from src.models import NOAAObservation, Trade
from src.resolver import _calculate_outcome, resolve_trades


def _make_trade(
    trade_id: str = "abc123",
    market_id: str = "test-market-1",
    side: str = "YES",
    price: str = "0.60",
    size: str = "25.00",
) -> Trade:
    """Create a test Trade."""
    return Trade(
        trade_id=trade_id,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        size=Decimal(size),
        noaa_probability=Decimal("0.80"),
        edge=Decimal("0.20"),
        timestamp=datetime.now(tz=UTC),
        status="filled",
    )


def _make_observation(
    temp_high: float | None = 80.0,
    temp_low: float | None = 55.0,
    precipitation: float | None = 0.0,
    observation_date: date | None = None,
) -> NOAAObservation:
    """Create a test NOAAObservation."""
    return NOAAObservation(
        station_id="KNYC",
        location="40.71,-74.01",
        observation_date=observation_date or date.today() - timedelta(days=1),
        retrieved_at=datetime.now(tz=UTC),
        temperature_high=temp_high,
        temperature_low=temp_low,
        precipitation=precipitation,
    )


class TestCalculateOutcome:
    """Tests for _calculate_outcome."""

    def test_yes_trade_wins_when_condition_met(self) -> None:
        """YES trade on 'above' wins when actual temp exceeds threshold."""
        trade = _make_trade(side="YES", price="0.60", size="25.00")
        obs = _make_observation(temp_high=80.0)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        assert outcome == "won"
        assert pnl == Decimal("10.00")  # (1.00 - 0.60) * 25

    def test_yes_trade_loses_when_condition_not_met(self) -> None:
        """YES trade on 'above' loses when actual temp below threshold."""
        trade = _make_trade(side="YES", price="0.60", size="25.00")
        obs = _make_observation(temp_high=70.0)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        assert outcome == "lost"
        assert pnl == Decimal("-15.00")  # (0.00 - 0.60) * 25

    def test_no_trade_wins_when_condition_not_met(self) -> None:
        """NO trade on 'above' wins when actual temp below threshold."""
        trade = _make_trade(side="NO", price="0.40", size="25.00")
        obs = _make_observation(temp_high=70.0)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        assert outcome == "won"
        assert pnl == Decimal("15.00")  # (1.00 - 0.40) * 25

    def test_no_trade_loses_when_condition_met(self) -> None:
        """NO trade on 'above' loses when actual temp exceeds threshold."""
        trade = _make_trade(side="NO", price="0.40", size="25.00")
        obs = _make_observation(temp_high=80.0)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        assert outcome == "lost"
        assert pnl == Decimal("-10.00")  # (0.00 - 0.40) * 25

    def test_below_comparison(self) -> None:
        """Correctly handles 'below' comparison."""
        trade = _make_trade(side="YES", price="0.50", size="20.00")
        obs = _make_observation(temp_low=30.0)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="temperature_low",
            threshold=32.0,
            comparison="below",
        )

        assert outcome == "won"
        assert pnl == Decimal("10.00")  # (1.00 - 0.50) * 20

    def test_precipitation_uses_actual_inches(self) -> None:
        """Precipitation uses actual measured inches, not PoP."""
        trade = _make_trade(side="YES", price="0.50", size="20.00")
        obs = _make_observation(precipitation=0.5)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="precipitation",
            threshold=0.1,
            comparison="above",
        )

        assert outcome == "won"

    def test_returns_none_when_no_data(self) -> None:
        """Returns (None, None) when observation lacks required metric."""
        trade = _make_trade()
        obs = _make_observation(temp_high=None, temp_low=None)

        outcome, pnl = _calculate_outcome(
            trade=trade,
            observation=obs,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        assert outcome is None
        assert pnl is None


class TestResolveTradesSkipsFuture:
    """Tests that resolve_trades skips future-dated events."""

    def test_skips_future_event_date(self, tmp_path: Path) -> None:
        """Trades with future event dates are skipped, not resolved."""
        journal = Journal(db_path=tmp_path / "test.db")
        noaa = MagicMock()

        # Create a trade
        trade = _make_trade(market_id="future-market")
        journal.log_trade(trade)
        journal.update_trade_status(trade.trade_id, "filled")

        # Cache market with future event date
        future_date = date.today() + timedelta(days=5)
        journal.cache_market(
            market_id="future-market",
            location="New York",
            lat=40.7128,
            lon=-74.006,
            event_date=future_date,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        stats = resolve_trades(journal, noaa)

        assert stats["resolved_count"] == 0
        assert stats["skipped_future"] == 1
        # NOAA should NOT have been called at all
        noaa.get_observations.assert_not_called()

        journal.close()

    def test_resolves_past_event_date(self, tmp_path: Path) -> None:
        """Trades with past event dates are resolved using observations."""
        journal = Journal(db_path=tmp_path / "test.db")
        noaa = MagicMock()

        past_date = date.today() - timedelta(days=2)
        obs = _make_observation(temp_high=80.0, observation_date=past_date)
        noaa.get_observations.return_value = obs

        # Create a trade
        trade = _make_trade(market_id="past-market")
        journal.log_trade(trade)
        journal.update_trade_status(trade.trade_id, "filled")

        # Cache market with past event date
        journal.cache_market(
            market_id="past-market",
            location="New York",
            lat=40.7128,
            lon=-74.006,
            event_date=past_date,
            metric="temperature_high",
            threshold=75.0,
            comparison="above",
        )

        stats = resolve_trades(journal, noaa)

        assert stats["resolved_count"] == 1
        assert stats["wins"] == 1
        noaa.get_observations.assert_called_once()

        journal.close()


class TestDuplicateTradesPrevention:
    """Tests that Journal.has_open_trade prevents duplicate trades."""

    def test_no_open_trade_returns_false(self, tmp_path: Path) -> None:
        """Returns False when no open trade exists for market."""
        journal = Journal(db_path=tmp_path / "test.db")
        assert journal.has_open_trade("nonexistent-market") is False
        journal.close()

    def test_pending_trade_detected(self, tmp_path: Path) -> None:
        """Detects pending trades as open."""
        journal = Journal(db_path=tmp_path / "test.db")
        trade = _make_trade(market_id="market-1")
        journal.log_trade(trade)
        # Trade is pending by default
        assert journal.has_open_trade("market-1") is True
        journal.close()

    def test_filled_trade_detected(self, tmp_path: Path) -> None:
        """Detects filled trades as open."""
        journal = Journal(db_path=tmp_path / "test.db")
        trade = _make_trade(market_id="market-2")
        journal.log_trade(trade)
        journal.update_trade_status(trade.trade_id, "filled")
        assert journal.has_open_trade("market-2") is True
        journal.close()

    def test_resolved_trade_not_blocking(self, tmp_path: Path) -> None:
        """Resolved trades don't block new trades on the same market."""
        journal = Journal(db_path=tmp_path / "test.db")
        trade = _make_trade(market_id="market-3")
        journal.log_trade(trade)
        journal.update_trade_resolution(trade.trade_id, "won", Decimal("10.00"))
        assert journal.has_open_trade("market-3") is False
        journal.close()

    def test_cancelled_trade_not_blocking(self, tmp_path: Path) -> None:
        """Cancelled trades don't block new trades on the same market."""
        journal = Journal(db_path=tmp_path / "test.db")
        trade = _make_trade(market_id="market-4")
        journal.log_trade(trade)
        journal.update_trade_status(trade.trade_id, "cancelled")
        assert journal.has_open_trade("market-4") is False
        journal.close()
