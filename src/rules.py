"""Rule-based signal generation for extreme market mispricings.

Implements gopfan2-style rules: buy YES when severely underpriced,
buy NO when severely overpriced, with NOAA directional confirmation.
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from src.models import Signal, WeatherMarket
from src.sizing import calculate_kelly

logger = structlog.get_logger()

# Extreme value thresholds
_EXTREME_LOW_PRICE = Decimal("0.15")
_EXTREME_HIGH_PRICE = Decimal("0.85")
# Reduced Kelly for rule-only signals (half of quarter-Kelly)
_RULE_KELLY_MULTIPLIER = Decimal("0.125")


def evaluate_extreme_value(
    market: WeatherMarket,
    noaa_probability: Decimal | None,
    bankroll: Decimal,
    min_edge: Decimal = Decimal("0.10"),
) -> Signal | None:
    """Check if a market has extreme mispricing with NOAA confirmation.

    Rules:
    - Buy YES when market price < $0.15 AND NOAA probability > 0.50
    - Buy NO when market price > $0.85 AND NOAA probability < 0.50

    Both rules require NOAA directional confirmation to avoid pure
    contrarian plays. Uses reduced Kelly (0.125) since the signal
    relies partly on structural mispricing rather than pure data edge.

    Args:
        market: Weather market to evaluate.
        noaa_probability: NOAA-estimated probability (0-1), or None.
        bankroll: Current bankroll for sizing.
        min_edge: Minimum edge for standard Kelly calculation.

    Returns:
        Signal if extreme value detected, None otherwise.
    """
    if noaa_probability is None:
        return None

    signal: Signal | None = None

    if market.yes_price < _EXTREME_LOW_PRICE and noaa_probability > Decimal("0.50"):
        # Extremely underpriced YES with NOAA confirming high probability
        edge = noaa_probability - market.yes_price
        kelly_frac, recommended_size = calculate_kelly(
            noaa_probability=noaa_probability,
            market_price=market.yes_price,
            bankroll=bankroll,
            kelly_multiplier=_RULE_KELLY_MULTIPLIER,
            min_edge=Decimal("0.01"),  # Lower threshold for rule-based signals
        )
        if recommended_size > Decimal("0"):
            signal = Signal(
                market_id=market.market_id,
                noaa_probability=noaa_probability,
                market_price=market.yes_price,
                edge=edge,
                side="YES",
                kelly_fraction=kelly_frac,
                recommended_size=recommended_size,
                confidence="high",  # Extreme mispricing = high confidence
            )
            logger.info(
                "extreme_value_signal",
                market_id=market.market_id,
                side="YES",
                price=str(market.yes_price),
                noaa_prob=str(noaa_probability),
                edge=str(edge),
            )

    elif market.yes_price > _EXTREME_HIGH_PRICE and noaa_probability < Decimal("0.50"):
        # Extremely overpriced YES — buy NO with NOAA confirming low probability
        no_price = Decimal("1") - market.yes_price
        no_prob = Decimal("1") - noaa_probability
        edge = no_prob - no_price
        kelly_frac, recommended_size = calculate_kelly(
            noaa_probability=noaa_probability,
            market_price=market.yes_price,
            bankroll=bankroll,
            kelly_multiplier=_RULE_KELLY_MULTIPLIER,
            min_edge=Decimal("0.01"),
        )
        if recommended_size > Decimal("0"):
            signal = Signal(
                market_id=market.market_id,
                noaa_probability=noaa_probability,
                market_price=market.yes_price,
                edge=noaa_probability - market.yes_price,
                side="NO",
                kelly_fraction=kelly_frac,
                recommended_size=recommended_size,
                confidence="high",
            )
            logger.info(
                "extreme_value_signal",
                market_id=market.market_id,
                side="NO",
                price=str(market.yes_price),
                noaa_prob=str(noaa_probability),
                edge=str(edge),
            )

    return signal
