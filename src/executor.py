"""Trading execution interface for simulated and live trading.

Abstracts trade execution so the simulator can swap between paper
trading and live Polymarket execution without code changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from src.models import Signal, Trade

if TYPE_CHECKING:
    from decimal import Decimal

logger = structlog.get_logger()


class TradeExecutor(ABC):
    """Interface for executing trades (simulated or live)."""

    @abstractmethod
    def execute(
        self,
        signal: Signal,
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
    """Paper trading executor — fills immediately at signal price."""

    def execute(
        self,
        signal: Signal,
        trade_size: Decimal,
    ) -> Trade | None:
        """Execute a simulated trade at the signal price.

        Args:
            signal: Trading signal to execute.
            trade_size: Dollar amount to trade.

        Returns:
            Filled Trade record.
        """
        trade = Trade(
            market_id=signal.market_id,
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
            market_id=trade.market_id,
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


# Placeholder for V1 live trading:
#
# class LiveExecutor(TradeExecutor):
#     """Live Polymarket executor using py-clob-client."""
#
#     def __init__(self, client: ClobClient) -> None:
#         self._client = client
#
#     def execute(self, signal: Signal, trade_size: Decimal) -> Trade | None:
#         # Place real order via py-clob-client
#         # Monitor for fill
#         # Return actual fill price and size
#         ...
#
#     def get_current_price(self, market_id: str) -> Decimal | None:
#         # Fetch real-time midpoint from CLOB
#         ...
