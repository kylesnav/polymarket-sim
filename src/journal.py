"""SQLite trade journal — connection owner and public API.

Delegates schema management to src.schema and SQL queries to src.queries.
All monetary values stored as TEXT (Decimal string representation) for precision.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Generator
    from datetime import date
    from decimal import Decimal

    from src.models import Trade, WeatherEvent
from src.queries import (
    backfill_trade_context,
    cache_event,
    cache_market,
    get_daily_pnl,
    get_event_metadata,
    get_lifecycle_counts,
    get_market_metadata,
    get_open_position_size,
    get_open_positions_with_pnl,
    get_portfolio_summary,
    get_report_data,
    get_snapshots,
    get_trade_detail,
    get_trade_history,
    get_trades_by_event,
    get_trades_with_context,
    get_unresolved_trades,
    has_open_trade,
    insert_trade,
    save_daily_snapshot,
    update_trade_resolution,
    update_trade_status,
)
from src.schema import initialize_schema

logger = structlog.get_logger()

DEFAULT_DB_PATH = Path("data/trades.db")


class Journal:
    """SQLite-backed trade journal.

    Owns the database connection and delegates all queries to src.queries.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        """Initialize the journal and create tables.

        Args:
            db_path: Path to the SQLite database file.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        initialize_schema(self._conn)
        logger.info("journal_initialized", db_path=str(db_path))

    @property
    def connection(self) -> sqlite3.Connection:
        """Get the underlying database connection.

        Returns:
            The SQLite connection object.
        """
        return self._conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for atomic database operations.

        On success, commits. On exception, rolls back and re-raises.

        Yields:
            The SQLite connection for use within the transaction.
        """
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def log_trade(
        self,
        trade: Trade,
        market_context: dict[str, object] | None = None,
    ) -> bool:
        """Log a trade to the database.

        Args:
            trade: Trade record to log.
            market_context: Optional market metadata to store alongside.

        Returns:
            True if logged successfully, False on error.
        """
        return insert_trade(self._conn, trade, market_context)

    def has_open_trade(self, market_id: str) -> bool:
        """Check if a market already has an open trade.

        Args:
            market_id: Market ID to check.

        Returns:
            True if an open trade exists for this market.
        """
        return has_open_trade(self._conn, market_id)

    def get_open_position_size(self, market_id: str) -> Decimal:
        """Get total size of open trades for a market.

        Args:
            market_id: Market ID to check.

        Returns:
            Total position size in dollars, or zero if no open trades.
        """
        return get_open_position_size(self._conn, market_id)

    def update_trade_status(self, trade_id: str, status: str) -> bool:
        """Update the status of a trade.

        Args:
            trade_id: ID of the trade to update.
            status: New status value.

        Returns:
            True if updated successfully.
        """
        return update_trade_status(self._conn, trade_id, status)

    def update_trade_resolution(
        self,
        trade_id: str,
        outcome: str,
        actual_pnl: Decimal,
        actual_value: float | None = None,
        actual_value_unit: str = "",
    ) -> bool:
        """Update a trade with resolution outcome and actual P&L.

        Args:
            trade_id: ID of the trade to resolve.
            outcome: "won" or "lost".
            actual_pnl: Actual profit/loss from the trade.
            actual_value: The actual observed weather value.
            actual_value_unit: Unit for the actual value.

        Returns:
            True if updated successfully.
        """
        return update_trade_resolution(
            self._conn, trade_id, outcome, actual_pnl, actual_value, actual_value_unit
        )

    def get_unresolved_trades(self) -> list[Trade]:
        """Get all filled trades that have not been resolved.

        Returns:
            List of unresolved Trade records.
        """
        return get_unresolved_trades(self._conn)

    def get_daily_pnl(self, target_date: date) -> Decimal:
        """Get the total P&L for a specific date.

        Args:
            target_date: Date to query.

        Returns:
            Daily P&L as Decimal.
        """
        return get_daily_pnl(self._conn, target_date)

    def save_daily_snapshot(
        self,
        snapshot_date: date,
        cash: Decimal,
        total_value: Decimal,
        daily_pnl: Decimal,
        open_positions: int,
        trades_today: int,
    ) -> None:
        """Save or update a daily portfolio snapshot.

        Args:
            snapshot_date: Date of the snapshot.
            cash: Cash balance.
            total_value: Total portfolio value.
            daily_pnl: P&L for the day.
            open_positions: Number of open positions.
            trades_today: Number of trades executed today.
        """
        save_daily_snapshot(
            self._conn, snapshot_date, cash, total_value, daily_pnl,
            open_positions, trades_today,
        )

    def get_trade_history(self, days: int = 30) -> list[Trade]:
        """Get trade history for the last N days.

        Args:
            days: Number of days of history to retrieve.

        Returns:
            List of Trade records.
        """
        return get_trade_history(self._conn, days)

    def get_trades_with_context(
        self,
        days: int = 90,
        status: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, object]]:
        """Get trades with market context and lifecycle state.

        Args:
            days: Number of days of history.
            status: Optional status filter.
            outcome: Optional outcome filter.

        Returns:
            List of enriched trade dicts with lifecycle field.
        """
        return get_trades_with_context(self._conn, days, status, outcome)

    def get_trade_detail(self, trade_id: str) -> dict[str, object] | None:
        """Get a single trade with full context.

        Args:
            trade_id: Trade ID to look up.

        Returns:
            Enriched trade dict or None if not found.
        """
        return get_trade_detail(self._conn, trade_id)

    def get_lifecycle_counts(self) -> dict[str, int]:
        """Get counts of trades by lifecycle state.

        Returns:
            Dict with open, ready, resolved, total counts.
        """
        return get_lifecycle_counts(self._conn)

    def get_portfolio_summary(self, starting_bankroll: Decimal) -> dict[str, object]:
        """Compute portfolio state from trade history.

        Args:
            starting_bankroll: The starting bankroll amount.

        Returns:
            Dict with cash, exposure, total_value, actual_pnl, and lifecycle counts.
        """
        return get_portfolio_summary(self._conn, starting_bankroll)

    def backfill_trade_context(self) -> None:
        """Backfill context columns from markets cache for existing trades."""
        backfill_trade_context(self._conn)

    def cache_market(
        self,
        market_id: str,
        location: str,
        lat: float,
        lon: float,
        event_date: date,
        metric: str,
        threshold: float,
        comparison: str,
    ) -> bool:
        """Cache market metadata for later resolution.

        Args:
            market_id: Unique market ID.
            location: Location name.
            lat: Latitude.
            lon: Longitude.
            event_date: Target event date.
            metric: Metric type.
            threshold: Threshold value.
            comparison: Comparison type.

        Returns:
            True if cached successfully.
        """
        return cache_market(
            self._conn, market_id, location, lat, lon,
            event_date, metric, threshold, comparison,
        )

    def get_market_metadata(self, market_id: str) -> dict[str, object] | None:
        """Retrieve cached market metadata.

        Args:
            market_id: Market ID to look up.

        Returns:
            Dict with market metadata or None if not found.
        """
        return get_market_metadata(self._conn, market_id)

    def get_snapshots(self, days: int = 60) -> list[dict[str, object]]:
        """Get daily snapshots for the last N days.

        Args:
            days: Number of days of snapshots to retrieve.

        Returns:
            List of snapshot dicts ordered by date ascending.
        """
        return get_snapshots(self._conn, days)

    def get_open_positions_with_pnl(self) -> dict[str, Any]:
        """Get all open positions with P&L estimates.

        Returns:
            Dict with "positions" list and "summary" aggregates.
        """
        return get_open_positions_with_pnl(self._conn)

    def get_report_data(self, days: int = 30) -> dict[str, Any]:
        """Get summary report data for the last N days.

        Args:
            days: Number of days to include in report.

        Returns:
            Dict with summary statistics.
        """
        return get_report_data(self._conn, days)

    def cache_event(self, event: WeatherEvent) -> bool:
        """Cache a multi-outcome weather event's metadata.

        Args:
            event: WeatherEvent to cache.

        Returns:
            True if cached successfully.
        """
        return cache_event(self._conn, event)

    def get_event_metadata(self, event_id: str) -> dict[str, object] | None:
        """Retrieve cached event metadata.

        Args:
            event_id: Event ID to look up.

        Returns:
            Dict with event metadata or None if not found.
        """
        return get_event_metadata(self._conn, event_id)

    def get_trades_by_event(self, event_id: str) -> list[dict[str, object]]:
        """Get all trades for a specific event.

        Args:
            event_id: Event ID to query.

        Returns:
            List of enriched trade dicts for the event.
        """
        return get_trades_by_event(self._conn, event_id)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
