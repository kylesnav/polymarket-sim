"""Tests for quarter-Kelly position sizing."""

from decimal import Decimal

from src.sizing import calculate_kelly


class TestCalculateKelly:
    """Tests for the calculate_kelly function."""

    def test_returns_zero_when_no_edge(self) -> None:
        """Kelly returns (0, 0) when NOAA probability equals market price."""
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.50"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("500"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

    def test_returns_zero_when_edge_below_threshold(self) -> None:
        """Kelly returns (0, 0) when edge is below minimum threshold."""
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.55"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("500"),
            min_edge=Decimal("0.10"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

    def test_never_returns_negative(self) -> None:
        """Kelly fraction and size are never negative."""
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.30"),
            market_price=Decimal("0.70"),
            bankroll=Decimal("500"),
        )
        assert fraction >= Decimal("0")
        assert size >= Decimal("0")

    def test_applies_quarter_kelly_multiplier(self) -> None:
        """Kelly fraction is multiplied by 0.25."""
        # NOAA=0.80, market=0.50 → edge=0.30
        # Full Kelly = 0.30 / (1 - 0.50) = 0.60
        # Quarter Kelly = 0.60 * 0.25 = 0.15
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.80"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("1000"),
            kelly_multiplier=Decimal("0.25"),
        )
        assert fraction == Decimal("0.15")
        assert size == Decimal("150.00")

    def test_yes_signal_sizing(self) -> None:
        """Correct sizing for a YES-side signal."""
        # NOAA=0.85, market=0.65 → edge=0.20
        # Full Kelly = 0.20 / 0.35 ≈ 0.5714
        # Quarter Kelly ≈ 0.1429
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.85"),
            market_price=Decimal("0.65"),
            bankroll=Decimal("500"),
        )
        assert fraction > Decimal("0")
        assert size > Decimal("0")
        # Quarter Kelly should be roughly 0.25 * 0.5714 ≈ 0.1429
        assert Decimal("0.14") <= fraction <= Decimal("0.15")

    def test_no_signal_sizing(self) -> None:
        """Correct sizing for a NO-side signal (market overpriced)."""
        # NOAA=0.30, market=0.60 → edge=-0.30 (abs > threshold)
        # NO perspective: p_no=0.70, q_no=0.40, no_edge=0.30
        # Full Kelly = 0.30 / (1 - 0.40) = 0.50
        # Quarter Kelly = 0.125
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.30"),
            market_price=Decimal("0.60"),
            bankroll=Decimal("500"),
        )
        assert fraction > Decimal("0")
        assert size > Decimal("0")

    def test_returns_zero_for_invalid_probability(self) -> None:
        """Kelly returns (0, 0) for out-of-range NOAA probability."""
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("500"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

        fraction, size = calculate_kelly(
            noaa_probability=Decimal("1"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("500"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

    def test_returns_zero_for_invalid_market_price(self) -> None:
        """Kelly returns (0, 0) for out-of-range market price."""
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.80"),
            market_price=Decimal("0"),
            bankroll=Decimal("500"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.80"),
            market_price=Decimal("1"),
            bankroll=Decimal("500"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

    def test_returns_zero_for_zero_bankroll(self) -> None:
        """Kelly returns (0, 0) when bankroll is zero or negative."""
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.80"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("0"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

    def test_custom_kelly_multiplier(self) -> None:
        """Supports custom Kelly multiplier values."""
        fraction_quarter, _ = calculate_kelly(
            noaa_probability=Decimal("0.80"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("1000"),
            kelly_multiplier=Decimal("0.25"),
        )
        fraction_half, _ = calculate_kelly(
            noaa_probability=Decimal("0.80"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("1000"),
            kelly_multiplier=Decimal("0.50"),
        )
        assert fraction_half > fraction_quarter

    def test_custom_min_edge(self) -> None:
        """Supports custom minimum edge threshold."""
        # Edge is 0.15, which is below 0.20 threshold
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.65"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("500"),
            min_edge=Decimal("0.20"),
        )
        assert fraction == Decimal("0")
        assert size == Decimal("0")

        # Same edge, but threshold is 0.10 — should pass
        fraction, size = calculate_kelly(
            noaa_probability=Decimal("0.65"),
            market_price=Decimal("0.50"),
            bankroll=Decimal("500"),
            min_edge=Decimal("0.10"),
        )
        assert fraction > Decimal("0")
        assert size > Decimal("0")
