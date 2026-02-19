"""Tests for the trading executor interface."""

from __future__ import annotations

from decimal import Decimal

from src.executor import SimulatedExecutor
from src.models import Signal


def _make_signal(
    market_id: str = "mkt-1",
    side: str = "YES",
) -> Signal:
    return Signal(
        market_id=market_id,
        noaa_probability=Decimal("0.65"),
        market_price=Decimal("0.50"),
        edge=Decimal("0.15"),
        side=side,
        kelly_fraction=Decimal("0.08"),
        recommended_size=Decimal("10"),
        confidence="medium",
    )


class TestSimulatedExecutor:
    """Tests for SimulatedExecutor."""

    def test_execute_returns_filled_trade(self) -> None:
        executor = SimulatedExecutor()
        signal = _make_signal()
        trade = executor.execute(signal, Decimal("10"))
        assert trade is not None
        assert trade.status == "filled"
        assert trade.market_id == "mkt-1"
        assert trade.size == Decimal("10")
        assert trade.side == "YES"

    def test_execute_uses_signal_price(self) -> None:
        executor = SimulatedExecutor()
        signal = _make_signal()
        trade = executor.execute(signal, Decimal("25"))
        assert trade is not None
        assert trade.price == Decimal("0.50")

    def test_get_current_price_returns_none(self) -> None:
        executor = SimulatedExecutor()
        assert executor.get_current_price("mkt-1") is None

    def test_execute_preserves_signal_data(self) -> None:
        executor = SimulatedExecutor()
        signal = _make_signal(side="NO")
        trade = executor.execute(signal, Decimal("15"))
        assert trade is not None
        assert trade.side == "NO"
        assert trade.noaa_probability == Decimal("0.65")
        assert trade.edge == Decimal("0.15")
