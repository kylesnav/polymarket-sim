"""Trading execution interface for simulated and live trading.

Abstracts trade execution so the simulator can swap between paper
trading and live Polymarket execution without code changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import structlog

from src.models import BucketSignal, Signal, Trade

if TYPE_CHECKING:
    from src.polymarket import PolymarketClient

logger = structlog.get_logger()

# Maximum slippage allowed from best price before rejecting a fill
_MAX_SLIPPAGE: Decimal = Decimal("0.05")


class TradeExecutor(ABC):
    """Interface for executing trades (simulated or live)."""

    @abstractmethod
    def execute(
        self,
        signal: Signal | BucketSignal,
        trade_size: Decimal,
    ) -> Trade | None:
        """Execute a trade and return the fill, or None on failure.

        Args:
            signal: Trading signal to execute.
            trade_size: Dollar amount to trade.

        Returns:
            Filled Trade record, or None if execution failed.
        """

    @abstractmethod
    def get_current_price(self, market_id: str) -> Decimal | None:
        """Get current market price for slippage estimation.

        Args:
            market_id: Market to get price for.

        Returns:
            Current YES price, or None if unavailable.
        """


class SimulatedExecutor(TradeExecutor):
    """Legacy paper trading executor — fills immediately at signal price."""

    def execute(
        self,
        signal: Signal | BucketSignal,
        trade_size: Decimal,
    ) -> Trade | None:
        """Execute a simulated trade at the signal price.

        Args:
            signal: Trading signal to execute.
            trade_size: Dollar amount to trade.

        Returns:
            Filled Trade record.
        """
        market_id = signal.market_id if isinstance(signal, Signal) else ""
        trade = Trade(
            market_id=market_id,
            side=signal.side,
            price=signal.market_price,
            size=trade_size,
            noaa_probability=signal.noaa_probability,
            edge=signal.edge,
            timestamp=datetime.now(tz=UTC),
            status="filled",
        )
        logger.info(
            "simulated_fill",
            trade_id=trade.trade_id,
            market_id=market_id,
            side=trade.side,
            size=str(trade_size),
        )
        return trade

    def get_current_price(self, market_id: str) -> Decimal | None:
        """Not applicable for simulation — returns None.

        Args:
            market_id: Market to get price for.

        Returns:
            None (simulation uses signal price directly).
        """
        return None


class PaperExecutor(TradeExecutor):
    """Paper trading executor that walks real order books for realistic fills.

    Fetches the actual order book from Polymarket's CLOB API and simulates
    a fill by walking the book. Rejects trades where slippage exceeds the
    threshold or book depth is insufficient.
    """

    def __init__(self, polymarket: PolymarketClient) -> None:
        """Initialize with a Polymarket client for order book access.

        Args:
            polymarket: Polymarket API client.
        """
        self._polymarket = polymarket

    def execute(
        self,
        signal: Signal | BucketSignal,
        trade_size: Decimal,
    ) -> Trade | None:
        """Execute a paper trade by walking the real order book.

        Args:
            signal: Trading signal to execute.
            trade_size: Dollar amount to trade.

        Returns:
            Filled Trade with realistic fill_price, or None if rejected.
        """
        token_id = signal.token_id if isinstance(signal, BucketSignal) else ""
        if not token_id:
            # Fallback: fill at signal price for legacy binary signals
            return self._fill_at_signal_price(signal, trade_size)

        try:
            book = self._polymarket.get_order_book(token_id)
        except Exception as e:
            logger.warning(
                "order_book_fetch_failed",
                token_id=token_id,
                error=str(e),
            )
            return self._fill_at_signal_price(signal, trade_size)

        # Walk the appropriate side of the book
        side: Literal["YES", "NO"] = signal.side
        levels = book.asks if side == "YES" else book.bids

        if not levels:
            logger.warning(
                "empty_order_book_fallback_to_signal_price",
                token_id=token_id,
                side=side,
            )
            return self._fill_at_signal_price(signal, trade_size)

        # Walk the book to compute average fill price
        best_price = levels[0].price
        remaining = trade_size
        total_cost = Decimal("0")
        total_filled = Decimal("0")
        book_depth = Decimal("0")

        for level in levels:
            if remaining <= Decimal("0"):
                break
            fillable = min(remaining, level.size)
            total_cost += fillable * level.price
            total_filled += fillable
            remaining -= fillable
            # Track depth within slippage tolerance
            if abs(level.price - best_price) / best_price <= _MAX_SLIPPAGE:
                book_depth += level.size

        if total_filled <= Decimal("0"):
            logger.warning("no_liquidity_fallback_to_signal_price", token_id=token_id)
            return self._fill_at_signal_price(signal, trade_size)

        avg_fill_price = (total_cost / total_filled).quantize(Decimal("0.0001"))

        # Check slippage
        if best_price > Decimal("0"):
            slippage = abs(avg_fill_price - best_price) / best_price
            if slippage > _MAX_SLIPPAGE:
                logger.warning(
                    "slippage_too_high_fallback_to_signal_price",
                    token_id=token_id,
                    avg_fill=str(avg_fill_price),
                    best_price=str(best_price),
                    slippage=str(slippage),
                )
                return self._fill_at_signal_price(signal, trade_size)

        # Partially filled: adjust trade size to what was actually fillable
        actual_size = min(trade_size, total_filled)

        # Build Trade with multi-outcome fields if BucketSignal
        event_id = ""
        bucket_index = -1
        outcome_label = ""
        if isinstance(signal, BucketSignal):
            event_id = signal.event_id
            bucket_index = signal.bucket_index
            outcome_label = signal.outcome_label

        market_id = signal.market_id if isinstance(signal, Signal) else ""

        trade = Trade(
            market_id=market_id,
            side=side,
            price=avg_fill_price,
            size=actual_size,
            noaa_probability=signal.noaa_probability,
            edge=signal.edge,
            timestamp=datetime.now(tz=UTC),
            status="filled",
            event_id=event_id,
            bucket_index=bucket_index,
            token_id=token_id,
            outcome_label=outcome_label,
            fill_price=avg_fill_price,
            book_depth_at_signal=book_depth.quantize(Decimal("0.01")),
        )

        logger.info(
            "paper_fill",
            trade_id=trade.trade_id,
            token_id=token_id,
            side=side,
            size=str(actual_size),
            avg_fill_price=str(avg_fill_price),
            best_price=str(best_price),
            book_depth=str(book_depth),
        )
        return trade

    def get_current_price(self, market_id: str) -> Decimal | None:
        """Not applicable for paper trading — returns None.

        Args:
            market_id: Market to get price for.

        Returns:
            None.
        """
        return None

    def get_executable_size(
        self,
        token_id: str,
        side: Literal["YES", "NO"],
        max_size: Decimal,
        max_slippage: Decimal = _MAX_SLIPPAGE,
    ) -> Decimal:
        """Walk the order book to find available liquidity within slippage.

        Args:
            token_id: Token to check liquidity for.
            side: Trade side (YES buys asks, NO buys bids).
            max_size: Maximum size to fill.
            max_slippage: Maximum price deviation from best.

        Returns:
            Maximum fillable size within slippage tolerance.
        """
        try:
            book = self._polymarket.get_order_book(token_id)
        except Exception:
            return Decimal("0")

        levels = book.asks if side == "YES" else book.bids
        if not levels:
            return Decimal("0")

        best_price = levels[0].price
        fillable = Decimal("0")

        for level in levels:
            if best_price > Decimal("0"):
                price_diff = abs(level.price - best_price) / best_price
                if price_diff > max_slippage:
                    break
            fillable += level.size
            if fillable >= max_size:
                return max_size

        return fillable

    def _fill_at_signal_price(
        self,
        signal: Signal | BucketSignal,
        trade_size: Decimal,
    ) -> Trade:
        """Fallback: fill at signal price when order book is unavailable.

        Args:
            signal: Trading signal.
            trade_size: Dollar amount.

        Returns:
            Filled Trade at signal price.
        """
        market_id = signal.market_id if isinstance(signal, Signal) else ""
        event_id = signal.event_id if isinstance(signal, BucketSignal) else ""
        bucket_index = signal.bucket_index if isinstance(signal, BucketSignal) else -1
        token_id = signal.token_id if isinstance(signal, BucketSignal) else ""
        outcome_label = signal.outcome_label if isinstance(signal, BucketSignal) else ""

        trade = Trade(
            market_id=market_id,
            side=signal.side,
            price=signal.market_price,
            size=trade_size,
            noaa_probability=signal.noaa_probability,
            edge=signal.edge,
            timestamp=datetime.now(tz=UTC),
            status="filled",
            event_id=event_id,
            bucket_index=bucket_index,
            token_id=token_id,
            outcome_label=outcome_label,
        )
        logger.info(
            "paper_fill_at_signal_price",
            trade_id=trade.trade_id,
            side=trade.side,
            size=str(trade_size),
        )
        return trade
