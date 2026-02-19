"""Tests for the backtest module."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from src.backtest import _check_condition, _get_actual_value, _observation_to_forecast_proxy
from src.models import NOAAForecast, NOAAObservation, WeatherMarket
from src.strategy import compute_noaa_probability


@pytest.fixture
def sample_observation() -> NOAAObservation:
    """Create a sample observation for testing."""
    return NOAAObservation(
        station_id="KNYC",
        location="40.71,-74.01",
        observation_date=date(2026, 2, 15),
        retrieved_at=datetime(2026, 2, 16, tzinfo=UTC),
        temperature_high=72.5,
        temperature_low=45.2,
        precipitation=0.15,
    )


@pytest.fixture
def sample_market() -> WeatherMarket:
    """Create a sample weather market for testing."""
    return WeatherMarket(
        market_id="test_market_1",
        question="Will NYC high temp exceed 70°F on Feb 15?",
        location="New York",
        lat=40.7128,
        lon=-74.0060,
        event_date=date(2026, 2, 15),
        metric="temperature_high",
        threshold=70.0,
        comparison="above",
        yes_price=Decimal("0.60"),
        no_price=Decimal("0.40"),
        volume=Decimal("1000"),
        close_date=datetime(2026, 2, 16, tzinfo=UTC),
        token_id="test_token",
    )


class TestObservationToForecastProxy:
    """Tests for _observation_to_forecast_proxy."""

    def test_converts_temperatures(self, sample_observation: NOAAObservation) -> None:
        """Forecast proxy should preserve temperature values."""
        proxy = _observation_to_forecast_proxy(sample_observation)
        assert isinstance(proxy, NOAAForecast)
        assert proxy.temperature_high == 72.5
        assert proxy.temperature_low == 45.2

    def test_converts_precipitation_to_probability(self) -> None:
        """Non-zero precip should map to probability 1.0."""
        obs = NOAAObservation(
            station_id="KNYC",
            location="40.71,-74.01",
            observation_date=date(2026, 2, 15),
            retrieved_at=datetime(2026, 2, 16, tzinfo=UTC),
            temperature_high=72.5,
            temperature_low=45.2,
            precipitation=0.5,
        )
        proxy = _observation_to_forecast_proxy(obs)
        assert proxy.precip_probability == 1.0

    def test_zero_precip_maps_to_zero(self) -> None:
        """Zero precipitation should map to probability 0.0."""
        obs = NOAAObservation(
            station_id="KNYC",
            location="40.71,-74.01",
            observation_date=date(2026, 2, 15),
            retrieved_at=datetime(2026, 2, 16, tzinfo=UTC),
            temperature_high=72.5,
            temperature_low=45.2,
            precipitation=0.0,
        )
        proxy = _observation_to_forecast_proxy(obs)
        assert proxy.precip_probability == 0.0

    def test_preserves_date_and_location(self, sample_observation: NOAAObservation) -> None:
        """Proxy should carry forward date and location info."""
        proxy = _observation_to_forecast_proxy(sample_observation)
        assert proxy.forecast_date == sample_observation.observation_date
        assert proxy.location == sample_observation.location

    def test_rejects_non_observation(self) -> None:
        """Should raise TypeError for non-observation input."""
        with pytest.raises(TypeError):
            _observation_to_forecast_proxy("not an observation")  # type: ignore[arg-type]


class TestCheckCondition:
    """Tests for _check_condition."""

    def test_above_met(self) -> None:
        assert _check_condition(75.0, 70.0, "above") is True

    def test_above_not_met(self) -> None:
        assert _check_condition(65.0, 70.0, "above") is False

    def test_above_exact_threshold(self) -> None:
        assert _check_condition(70.0, 70.0, "above") is False

    def test_below_met(self) -> None:
        assert _check_condition(65.0, 70.0, "below") is True

    def test_below_not_met(self) -> None:
        assert _check_condition(75.0, 70.0, "below") is False

    def test_below_exact_threshold(self) -> None:
        assert _check_condition(70.0, 70.0, "below") is False

    def test_unknown_comparison(self) -> None:
        assert _check_condition(70.0, 70.0, "between") is False


class TestGetActualValue:
    """Tests for _get_actual_value."""

    def test_temperature_high(self, sample_observation: NOAAObservation) -> None:
        assert _get_actual_value(sample_observation, "temperature_high") == 72.5

    def test_temperature_low(self, sample_observation: NOAAObservation) -> None:
        assert _get_actual_value(sample_observation, "temperature_low") == 45.2

    def test_precipitation(self, sample_observation: NOAAObservation) -> None:
        assert _get_actual_value(sample_observation, "precipitation") == 0.15

    def test_snowfall(self, sample_observation: NOAAObservation) -> None:
        assert _get_actual_value(sample_observation, "snowfall") == 0.15

    def test_unknown_metric(self, sample_observation: NOAAObservation) -> None:
        assert _get_actual_value(sample_observation, "wind_speed") is None

    def test_non_observation(self) -> None:
        assert _get_actual_value("not an observation", "temperature_high") is None


class TestStrategyWithProxy:
    """Tests that the strategy correctly computes probability from observation proxies."""

    def test_temp_above_threshold_high_probability(
        self, sample_observation: NOAAObservation, sample_market: WeatherMarket
    ) -> None:
        """When actual temp (72.5) > threshold (70), proxy should give high probability."""
        proxy = _observation_to_forecast_proxy(sample_observation)
        prob = compute_noaa_probability(proxy, sample_market)
        assert prob is not None
        # With temp_high=72.5 and threshold=70, z=(70-72.5)/std ~= -0.83
        # P(X > 70) should be fairly high
        assert prob > 0.5

    def test_temp_well_below_threshold_low_probability(self) -> None:
        """When actual temp (50) << threshold (70), proxy should give low probability."""
        obs = NOAAObservation(
            station_id="KNYC",
            location="40.71,-74.01",
            observation_date=date(2026, 2, 15),
            retrieved_at=datetime(2026, 2, 16, tzinfo=UTC),
            temperature_high=50.0,
            temperature_low=35.0,
            precipitation=0.0,
        )
        market = WeatherMarket(
            market_id="test_market",
            question="Will NYC high temp exceed 70°F on Feb 15?",
            location="New York",
            lat=40.7128,
            lon=-74.0060,
            event_date=date(2026, 2, 15),
            metric="temperature_high",
            threshold=70.0,
            comparison="above",
            yes_price=Decimal("0.60"),
            no_price=Decimal("0.40"),
            volume=Decimal("1000"),
            close_date=datetime(2026, 2, 16, tzinfo=UTC),
        )
        proxy = _observation_to_forecast_proxy(obs)
        prob = compute_noaa_probability(proxy, market)
        assert prob is not None
        assert prob < 0.1
