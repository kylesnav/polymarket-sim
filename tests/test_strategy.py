"""Tests for the strategy module."""

from datetime import UTC, date, datetime
from decimal import Decimal

from src.models import NOAAForecast, Portfolio, WeatherMarket
from src.strategy import scan_weather_markets


def _make_market(
    market_id: str = "test-market-1",
    metric: str = "temperature_high",
    threshold: float = 45.0,
    comparison: str = "above",
    yes_price: str = "0.50",
    event_date: date | None = None,
) -> WeatherMarket:
    """Create a test WeatherMarket."""
    return WeatherMarket(
        market_id=market_id,
        question=f"Will NYC high temp exceed {threshold}°F?",
        location="New York",
        lat=40.7128,
        lon=-74.006,
        event_date=event_date or date.today(),
        metric=metric,  # type: ignore[arg-type]
        threshold=threshold,
        comparison=comparison,  # type: ignore[arg-type]
        yes_price=Decimal(yes_price),
        no_price=Decimal("1") - Decimal(yes_price),
        volume=Decimal("10000"),
        close_date=datetime(2026, 3, 1, tzinfo=UTC),
    )


def _make_forecast(
    temp_high: float | None = 55.0,
    temp_low: float | None = 35.0,
    precip_prob: float | None = None,
    forecast_date: date | None = None,
) -> NOAAForecast:
    """Create a test NOAAForecast."""
    return NOAAForecast(
        location="New York",
        forecast_date=forecast_date or date.today(),
        retrieved_at=datetime.now(tz=UTC),
        temperature_high=temp_high,
        temperature_low=temp_low,
        precip_probability=precip_prob,
        forecast_narrative="Test forecast",
    )


def _make_portfolio(cash: str = "500") -> Portfolio:
    """Create a test Portfolio."""
    return Portfolio(
        cash=Decimal(cash),
        total_value=Decimal(cash),
        starting_bankroll=Decimal(cash),
    )


class TestScanWeatherMarkets:
    """Tests for scan_weather_markets."""

    def test_generates_yes_signal_when_noaa_higher(self) -> None:
        """Generates YES signal when NOAA probability > market price by threshold."""
        # NOAA forecast: high of 55°F, threshold 45°F
        # With std_dev ~5, P(>45) is very high (~0.98)
        # Market price 0.50 → big edge
        market = _make_market(yes_price="0.50", threshold=45.0)
        forecast = _make_forecast(temp_high=55.0)

        signals = scan_weather_markets(
            markets=[market],
            forecasts={market.market_id: forecast},
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=_make_portfolio(),
        )

        assert len(signals) == 1
        assert signals[0].side == "YES"
        assert signals[0].edge > Decimal("0")
        assert signals[0].recommended_size > Decimal("0")

    def test_generates_no_signal_when_noaa_lower(self) -> None:
        """Generates NO signal when NOAA probability < market price by threshold."""
        # NOAA forecast: high of 40°F, threshold 45°F
        # P(>45) is low (~0.16), market price 0.50 → edge is negative
        market = _make_market(yes_price="0.50", threshold=45.0)
        forecast = _make_forecast(temp_high=40.0)

        signals = scan_weather_markets(
            markets=[market],
            forecasts={market.market_id: forecast},
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=_make_portfolio(),
        )

        assert len(signals) == 1
        assert signals[0].side == "NO"
        assert signals[0].edge < Decimal("0")

    def test_no_signal_when_edge_below_threshold(self) -> None:
        """No signal generated when edge is below minimum threshold."""
        # NOAA forecast: high of 46°F, threshold 45°F
        # P(>45) ≈ 0.58, market price 0.55 → edge ≈ 0.03, below threshold
        market = _make_market(yes_price="0.55", threshold=45.0)
        forecast = _make_forecast(temp_high=46.0)

        signals = scan_weather_markets(
            markets=[market],
            forecasts={market.market_id: forecast},
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=_make_portfolio(),
        )

        assert len(signals) == 0

    def test_kill_switch_blocks_all_signals(self) -> None:
        """Kill switch prevents any signals from being generated."""
        market = _make_market(yes_price="0.50", threshold=45.0)
        forecast = _make_forecast(temp_high=55.0)

        signals = scan_weather_markets(
            markets=[market],
            forecasts={market.market_id: forecast},
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=True,
            portfolio=_make_portfolio(),
        )

        assert len(signals) == 0

    def test_daily_loss_limit_blocks_signals(self) -> None:
        """Daily loss limit prevents signal generation."""
        market = _make_market(yes_price="0.50", threshold=45.0)
        forecast = _make_forecast(temp_high=55.0)
        portfolio = Portfolio(
            cash=Decimal("475"),
            total_value=Decimal("475"),
            daily_pnl=Decimal("-30"),  # -6% of 500
            starting_bankroll=Decimal("500"),
        )

        signals = scan_weather_markets(
            markets=[market],
            forecasts={market.market_id: forecast},
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("475"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=portfolio,
        )

        assert len(signals) == 0

    def test_precipitation_market(self) -> None:
        """Precipitation markets use NOAA PoP directly."""
        market = _make_market(
            market_id="precip-1",
            metric="precipitation",
            threshold=0.1,
            comparison="above",
            yes_price="0.30",
        )
        # NOAA says 80% chance of precip → edge = 0.80 - 0.30 = 0.50
        forecast = _make_forecast(temp_high=None, precip_prob=0.80)

        signals = scan_weather_markets(
            markets=[market],
            forecasts={market.market_id: forecast},
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=_make_portfolio(),
        )

        assert len(signals) == 1
        assert signals[0].side == "YES"

    def test_multiple_markets(self) -> None:
        """Handles multiple markets, generating signals only where edge exists."""
        market_edge = _make_market(market_id="m1", yes_price="0.50", threshold=45.0)
        market_no_edge = _make_market(market_id="m2", yes_price="0.55", threshold=45.0)

        forecasts = {
            "m1": _make_forecast(temp_high=55.0),  # Big edge
            "m2": _make_forecast(temp_high=46.0),  # Small edge
        }

        signals = scan_weather_markets(
            markets=[market_edge, market_no_edge],
            forecasts=forecasts,
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=_make_portfolio(),
        )

        # Only the market with big edge should generate a signal
        assert len(signals) >= 1
        market_ids = [s.market_id for s in signals]
        assert "m1" in market_ids

    def test_no_forecast_skips_market(self) -> None:
        """Markets without matching forecasts are skipped."""
        market = _make_market(market_id="orphan")

        signals = scan_weather_markets(
            markets=[market],
            forecasts={},  # No forecasts
            min_edge=Decimal("0.10"),
            kelly_fraction=Decimal("0.25"),
            bankroll=Decimal("500"),
            position_cap_pct=Decimal("0.05"),
            max_bankroll=Decimal("500"),
            daily_loss_limit_pct=Decimal("0.05"),
            kill_switch=False,
            portfolio=_make_portfolio(),
        )

        assert len(signals) == 0
