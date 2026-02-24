"""Quarter-Kelly position sizing for binary and multi-outcome markets."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

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


def calculate_multi_outcome_kelly(
    bucket_probs: list[Decimal],
    market_prices: list[Decimal],
    bankroll: Decimal,
    kelly_multiplier: Decimal = Decimal("0.25"),
    min_edge: Decimal = Decimal("0.10"),
    max_buckets: int = 2,
    position_cap: Decimal | None = None,
) -> list[tuple[int, Literal["YES", "NO"], Decimal, Decimal]]:
    """Multi-outcome Kelly sizing across event buckets.

    Computes independent binary Kelly per bucket, ranks by edge,
    takes the top max_buckets, and budget-normalizes.

    Args:
        bucket_probs: NOAA probability for each bucket (sums to ~1.0).
        market_prices: Market YES price for each bucket.
        bankroll: Current bankroll in dollars.
        kelly_multiplier: Quarter-Kelly multiplier.
        min_edge: Minimum edge to consider a bucket tradeable.
        max_buckets: Maximum number of buckets to trade per event.
        position_cap: Maximum total position size for this event.

    Returns:
        List of (bucket_index, side, kelly_fraction, recommended_size) tuples
        for tradeable buckets, sorted by |edge| descending.
    """
    zero = Decimal("0")
    candidates: list[tuple[int, Literal["YES", "NO"], Decimal, Decimal, Decimal]] = []

    for i, (prob, price) in enumerate(zip(bucket_probs, market_prices, strict=True)):
        if prob <= zero or prob >= Decimal("1"):
            continue
        if price <= zero or price >= Decimal("1"):
            continue

        edge = prob - price
        abs_edge = abs(edge)

        if abs_edge < min_edge:
            continue

        kelly_frac, size = calculate_kelly(
            noaa_probability=prob,
            market_price=price,
            bankroll=bankroll,
            kelly_multiplier=kelly_multiplier,
            min_edge=min_edge,
        )

        if size <= zero:
            continue

        side: Literal["YES", "NO"] = "YES" if edge > zero else "NO"
        candidates.append((i, side, kelly_frac, size, abs_edge))

    # Rank by absolute edge, take top max_buckets
    candidates.sort(key=lambda c: c[4], reverse=True)
    selected = candidates[:max_buckets]

    if not selected:
        return []

    # Budget-normalize: if total exceeds position cap, scale down
    total_size = sum(c[3] for c in selected)
    cap = position_cap if position_cap is not None else bankroll * kelly_multiplier
    if total_size > cap and total_size > zero:
        scale = cap / total_size
        selected = [
            (idx, s, kf * scale, (sz * scale).quantize(Decimal("0.01")), e)
            for idx, s, kf, sz, e in selected
        ]

    result: list[tuple[int, Literal["YES", "NO"], Decimal, Decimal]] = [
        (idx, s, kf, sz) for idx, s, kf, sz, _e in selected
    ]

    logger.info(
        "multi_outcome_kelly",
        buckets_with_edge=len(candidates),
        buckets_selected=len(result),
        total_size=str(sum(r[3] for r in result)),
    )

    return result
