"""Quarter-Kelly position sizing for binary markets."""

from __future__ import annotations

from decimal import Decimal

import structlog

logger = structlog.get_logger()


def calculate_kelly(
    noaa_probability: Decimal,
    market_price: Decimal,
    bankroll: Decimal,
    kelly_multiplier: Decimal = Decimal("0.25"),
    min_edge: Decimal = Decimal("0.10"),
) -> tuple[Decimal, Decimal]:
    """Calculate quarter-Kelly position size for a binary market.

    Args:
        noaa_probability: Our estimated probability from NOAA data (0 to 1).
        market_price: Current YES price on Polymarket (0 to 1).
        bankroll: Current bankroll in dollars.
        kelly_multiplier: Fraction of full Kelly to use. Default 0.25.
        min_edge: Minimum edge required to generate a signal. Default 0.10.

    Returns:
        Tuple of (kelly_fraction, recommended_size_dollars).
        Returns (0, 0) if no edge or edge below threshold.
    """
    zero = Decimal("0")

    if noaa_probability <= zero or noaa_probability >= Decimal("1"):
        logger.warning("noaa_probability_out_of_range", probability=noaa_probability)
        return zero, zero

    if market_price <= zero or market_price >= Decimal("1"):
        logger.warning("market_price_out_of_range", price=market_price)
        return zero, zero

    if bankroll <= zero:
        logger.warning("bankroll_not_positive", bankroll=bankroll)
        return zero, zero

    edge = noaa_probability - market_price

    if abs(edge) < min_edge:
        logger.debug("edge_below_threshold", edge=edge, threshold=min_edge)
        return zero, zero

    if edge > zero:
        # Buy YES: Kelly = (p - q) / (1 - q) where p=NOAA prob, q=market price
        kelly_raw = edge / (Decimal("1") - market_price)
    else:
        # Buy NO: flip perspective — edge on the NO side
        # p_no = 1 - noaa_probability, q_no = 1 - market_price
        no_prob = Decimal("1") - noaa_probability
        no_price = Decimal("1") - market_price
        no_edge = no_prob - no_price
        if no_edge <= zero:
            return zero, zero
        kelly_raw = no_edge / (Decimal("1") - no_price)

    kelly_fraction = kelly_raw * kelly_multiplier

    # Clamp to [0, kelly_multiplier] — never bet more than the multiplier allows
    kelly_fraction = max(zero, min(kelly_fraction, kelly_multiplier))

    recommended_size = (kelly_fraction * bankroll).quantize(Decimal("0.01"))

    logger.info(
        "kelly_calculated",
        edge=edge,
        kelly_raw=kelly_raw,
        kelly_fraction=kelly_fraction,
        recommended_size=recommended_size,
    )

    return kelly_fraction, recommended_size
