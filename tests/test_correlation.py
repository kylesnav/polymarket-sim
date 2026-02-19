"""Tests for correlated position detection."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from src.correlation import (
    compute_correlated_exposure,
    find_correlated_markets,
    get_correlation_key,
)
from src.models import Signal, WeatherMarket


def _make_market(
    market_id: str = "mkt-1",
    location: str = "New York",
    metric: str = "temperature_high",
    event_date: date | None = None,
    threshold: float = 75.0,
) -> WeatherMarket:
    return WeatherMarket(
        market_id=market_id,
        question="test",
        location=location,
        lat=40.71,
        lon=-74.01,
        event_date=event_date or date(2027, 3, 5),
        metric=metric,
        threshold=threshold,
        comparison="above",
        yes_price=Decimal("0.50"),
        no_price=Decimal("0.50"),
        volume=Decimal("5000"),
        close_date=datetime(2027, 3, 5, tzinfo=UTC),
        token_id="tok",
    )


def _make_signal(market_id: str = "mkt-1") -> Signal:
    return Signal(
        market_id=market_id,
        noaa_probability=Decimal("0.65"),
        market_price=Decimal("0.50"),
        edge=Decimal("0.15"),
        side="YES",
        kelly_fraction=Decimal("0.08"),
        recommended_size=Decimal("10"),
        confidence="medium",
    )


class TestCorrelationKey:
    """Tests for get_correlation_key."""

    def test_same_location_metric_date(self) -> None:
        m1 = _make_market("m1", threshold=70.0)
        m2 = _make_market("m2", threshold=80.0)
        assert get_correlation_key(m1) == get_correlation_key(m2)

    def test_different_location(self) -> None:
        m1 = _make_market("m1", location="New York")
        m2 = _make_market("m2", location="Chicago")
        assert get_correlation_key(m1) != get_correlation_key(m2)

    def test_different_metric(self) -> None:
        m1 = _make_market("m1", metric="temperature_high")
        m2 = _make_market("m2", metric="temperature_low")
        assert get_correlation_key(m1) != get_correlation_key(m2)

    def test_different_date(self) -> None:
        m1 = _make_market("m1", event_date=date(2027, 3, 5))
        m2 = _make_market("m2", event_date=date(2027, 3, 6))
        assert get_correlation_key(m1) != get_correlation_key(m2)


class TestFindCorrelatedMarkets:
    """Tests for find_correlated_markets."""

    def test_finds_correlated_by_location_metric_date(self) -> None:
        m1 = _make_market("m1", threshold=70.0)
        m2 = _make_market("m2", threshold=80.0)
        signal = _make_signal("m1")
        result = find_correlated_markets(signal, [m1, m2])
        assert result == ["m2"]

    def test_excludes_self(self) -> None:
        m1 = _make_market("m1")
        signal = _make_signal("m1")
        result = find_correlated_markets(signal, [m1])
        assert result == []

    def test_returns_empty_when_no_correlation(self) -> None:
        m1 = _make_market("m1", location="New York")
        m2 = _make_market("m2", location="Chicago")
        signal = _make_signal("m1")
        result = find_correlated_markets(signal, [m1, m2])
        assert result == []

    def test_returns_empty_when_signal_market_not_found(self) -> None:
        m1 = _make_market("m1")
        signal = _make_signal("nonexistent")
        result = find_correlated_markets(signal, [m1])
        assert result == []


class TestComputeCorrelatedExposure:
    """Tests for compute_correlated_exposure."""

    def test_sums_correlated_positions(self) -> None:
        m1 = _make_market("m1", threshold=70.0)
        m2 = _make_market("m2", threshold=80.0)
        signal = _make_signal("m1")

        sizes = {"m1": Decimal("10"), "m2": Decimal("15")}
        total = compute_correlated_exposure(
            signal, [m1, m2], lambda mid: sizes.get(mid, Decimal("0"))
        )
        assert total == Decimal("25")

    def test_only_own_position_when_no_correlation(self) -> None:
        m1 = _make_market("m1", location="New York")
        m2 = _make_market("m2", location="Chicago")
        signal = _make_signal("m1")

        sizes = {"m1": Decimal("10"), "m2": Decimal("15")}
        total = compute_correlated_exposure(
            signal, [m1, m2], lambda mid: sizes.get(mid, Decimal("0"))
        )
        assert total == Decimal("10")
