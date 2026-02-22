"""Tests for extreme value rule-based signal generation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from src.models import WeatherMarket
from src.rules import evaluate_extreme_value


def _make_market(
    yes_price: Decimal = Decimal("0.10"),
    market_id: str = "mkt-1",
) -> WeatherMarket:
    return WeatherMarket(
        market_id=market_id,
        question="test",
        location="New York",
        lat=40.71,
        lon=-74.01,
        event_date=date(2027, 3, 5),
        metric="temperature_high",
        threshold=75.0,
        comparison="above",
        yes_price=yes_price,
        no_price=Decimal("1") - yes_price,
        volume=Decimal("5000"),
        close_date=datetime(2027, 3, 5, tzinfo=UTC),
        token_id="tok",
    )


class TestExtremeValueRules:
    """Tests for evaluate_extreme_value."""

    def test_buy_yes_when_underpriced_with_noaa_confirm(self) -> None:
        market = _make_market(yes_price=Decimal("0.10"))
        signal = evaluate_extreme_value(
            market, Decimal("0.60"), bankroll=Decimal("500"),
        )
        assert signal is not None
        assert signal.side == "YES"
        assert signal.confidence == "high"

    def test_no_signal_when_underpriced_but_noaa_disagrees(self) -> None:
        market = _make_market(yes_price=Decimal("0.10"))
        signal = evaluate_extreme_value(
            market, Decimal("0.08"), bankroll=Decimal("500"),
        )
        assert signal is None

    def test_buy_no_when_overpriced_with_noaa_confirm(self) -> None:
        market = _make_market(yes_price=Decimal("0.90"))
        signal = evaluate_extreme_value(
            market, Decimal("0.30"), bankroll=Decimal("500"),
        )
        assert signal is not None
        assert signal.side == "NO"

    def test_no_signal_when_overpriced_but_noaa_agrees(self) -> None:
        market = _make_market(yes_price=Decimal("0.90"))
        signal = evaluate_extreme_value(
            market, Decimal("0.80"), bankroll=Decimal("500"),
        )
        assert signal is None

    def test_no_signal_for_normal_price(self) -> None:
        market = _make_market(yes_price=Decimal("0.50"))
        signal = evaluate_extreme_value(
            market, Decimal("0.60"), bankroll=Decimal("500"),
        )
        assert signal is None

    def test_no_signal_when_noaa_is_none(self) -> None:
        market = _make_market(yes_price=Decimal("0.10"))
        signal = evaluate_extreme_value(
            market, None, bankroll=Decimal("500"),
        )
        assert signal is None

    def test_uses_reduced_kelly(self) -> None:
        market = _make_market(yes_price=Decimal("0.10"))
        signal = evaluate_extreme_value(
            market, Decimal("0.70"), bankroll=Decimal("500"),
        )
        assert signal is not None
        # Reduced kelly (0.125) should produce smaller sizes than quarter-kelly (0.25)
        assert signal.recommended_size > Decimal("0")
