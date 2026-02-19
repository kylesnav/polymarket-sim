"""SQLite trade journal for logging trades, positions, and daily snapshots."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import structlog

from src.models import Trade

logger = structlog.get_logger()

DEFAULT_DB_PATH = Path("data/trades.db")

CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    size TEXT NOT NULL,
    noaa_probability TEXT NOT NULL,
    edge TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    outcome TEXT,
    actual_pnl TEXT
)
"""

CREATE_POSITIONS_TABLE = """
CREATE TABLE IF NOT EXISTS positions (
    market_id TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    size TEXT NOT NULL,
    current_price TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    opened_at TEXT NOT NULL
)
"""

CREATE_DAILY_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    snapshot_date TEXT PRIMARY KEY,
    cash TEXT NOT NULL,
    total_value TEXT NOT NULL,
    daily_pnl TEXT NOT NULL,
    open_positions INTEGER NOT NULL,
    trades_today INTEGER NOT NULL
)
"""

CREATE_MARKETS_TABLE = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    event_date TEXT NOT NULL,
    metric TEXT NOT NULL,
    threshold REAL NOT NULL,
    comparison TEXT NOT NULL,
    cached_at TEXT NOT NULL
)
"""


class Journal:
    """SQLite-backed trade journal.

    Creates tables on init if they don't exist. All monetary values
    stored as TEXT (Decimal string representation) for precision.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        """Initialize the journal and create tables.

        Args:
            db_path: Path to the SQLite database file.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("journal_initialized", db_path=str(db_path))

    def _create_tables(self) -> None:
        """Create database tables if they don't exist."""
        cursor = self._conn.cursor()
        cursor.execute(CREATE_TRADES_TABLE)
        cursor.execute(CREATE_POSITIONS_TABLE)
        cursor.execute(CREATE_DAILY_SNAPSHOTS_TABLE)
        cursor.execute(CREATE_MARKETS_TABLE)
        self._conn.commit()

    def log_trade(self, trade: Trade) -> bool:
        """Log a trade to the database.

        Args:
            trade: Trade record to log.

        Returns:
            True if logged successfully, False on error.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """INSERT INTO trades
                   (trade_id, market_id, side, price, size,
                    noaa_probability, edge, timestamp, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade.trade_id,
                    trade.market_id,
                    trade.side,
                    str(trade.price),
                    str(trade.size),
                    str(trade.noaa_probability),
                    str(trade.edge),
                    trade.timestamp.isoformat(),
                    trade.status,
                ),
            )
            self._conn.commit()
            logger.info("trade_logged", trade_id=trade.trade_id, market_id=trade.market_id)
            return True
        except sqlite3.Error as e:
            logger.error("trade_log_failed", trade_id=trade.trade_id, error=str(e))
            return False

    def update_trade_status(self, trade_id: str, status: str) -> bool:
        """Update the status of a trade.

        Args:
            trade_id: ID of the trade to update.
            status: New status value.

        Returns:
            True if updated successfully.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE trades SET status = ? WHERE trade_id = ?",
                (status, trade_id),
            )
            self._conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("trade_update_failed", trade_id=trade_id, error=str(e))
            return False

    def update_trade_resolution(
        self, trade_id: str, outcome: str, actual_pnl: Decimal
    ) -> bool:
        """Update a trade with resolution outcome and actual P&L.

        Args:
            trade_id: ID of the trade to resolve.
            outcome: "won" or "lost".
            actual_pnl: Actual profit/loss from the trade.

        Returns:
            True if updated successfully.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                "UPDATE trades SET status = ?, outcome = ?, actual_pnl = ? WHERE trade_id = ?",
                ("resolved", outcome, str(actual_pnl), trade_id),
            )
            self._conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("trade_resolution_failed", trade_id=trade_id, error=str(e))
            return False

    def get_unresolved_trades(self) -> list[Trade]:
        """Get all filled trades that have not been resolved.

        Returns:
            List of unresolved Trade records.
        """
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT * FROM trades
               WHERE status = 'filled'
               ORDER BY timestamp ASC"""
        )
        rows = cursor.fetchall()
        trades: list[Trade] = []
        for row in rows:
            trades.append(
                Trade(
                    trade_id=str(row["trade_id"]),
                    market_id=str(row["market_id"]),
                    side=row["side"],  # type: ignore[arg-type]
                    price=Decimal(str(row["price"])),
                    size=Decimal(str(row["size"])),
                    noaa_probability=Decimal(str(row["noaa_probability"])),
                    edge=Decimal(str(row["edge"])),
                    timestamp=datetime.fromisoformat(str(row["timestamp"])),
                    status=row["status"],  # type: ignore[arg-type]
                    outcome=row["outcome"],  # type: ignore[arg-type]
                    actual_pnl=Decimal(str(row["actual_pnl"])) if row["actual_pnl"] else None,
                )
            )
        return trades

    def get_daily_pnl(self, target_date: date) -> Decimal:
        """Get the total P&L for a specific date.

        Args:
            target_date: Date to query.

        Returns:
            Daily P&L as Decimal.
        """
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT daily_pnl FROM daily_snapshots WHERE snapshot_date = ?",
            (target_date.isoformat(),),
        )
        row = cursor.fetchone()
        if row is not None:
            return Decimal(str(row["daily_pnl"]))
        return Decimal("0")

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
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """INSERT OR REPLACE INTO daily_snapshots
                   (snapshot_date, cash, total_value, daily_pnl,
                    open_positions, trades_today)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_date.isoformat(),
                    str(cash),
                    str(total_value),
                    str(daily_pnl),
                    open_positions,
                    trades_today,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("snapshot_save_failed", error=str(e))

    def get_trade_history(self, days: int = 30) -> list[Trade]:
        """Get trade history for the last N days.

        Args:
            days: Number of days of history to retrieve.

        Returns:
            List of Trade records.
        """
        cutoff = datetime.now(tz=UTC).isoformat()
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT * FROM trades
               WHERE timestamp >= date(?, ?)
               ORDER BY timestamp DESC""",
            (cutoff, f"-{days} days"),
        )
        rows = cursor.fetchall()
        trades: list[Trade] = []
        for row in rows:
            trades.append(
                Trade(
                    trade_id=str(row["trade_id"]),
                    market_id=str(row["market_id"]),
                    side=row["side"],  # type: ignore[arg-type]
                    price=Decimal(str(row["price"])),
                    size=Decimal(str(row["size"])),
                    noaa_probability=Decimal(str(row["noaa_probability"])),
                    edge=Decimal(str(row["edge"])),
                    timestamp=datetime.fromisoformat(str(row["timestamp"])),
                    status=row["status"],  # type: ignore[arg-type]
                    outcome=row["outcome"] if row["outcome"] else None,  # type: ignore[arg-type]
                    actual_pnl=Decimal(str(row["actual_pnl"])) if row["actual_pnl"] else None,
                )
            )
        return trades

    def get_snapshots(self, days: int = 60) -> list[dict[str, object]]:
        """Get daily snapshots for the last N days.

        Args:
            days: Number of days of snapshots to retrieve.

        Returns:
            List of snapshot dicts ordered by date ascending.
        """
        cursor = self._conn.cursor()
        cursor.execute(
            """SELECT * FROM daily_snapshots
               ORDER BY snapshot_date DESC
               LIMIT ?""",
            (days,),
        )
        rows = cursor.fetchall()
        return [
            {
                "snapshot_date": row["snapshot_date"],
                "cash": row["cash"],
                "total_value": row["total_value"],
                "daily_pnl": row["daily_pnl"],
                "open_positions": row["open_positions"],
                "trades_today": row["trades_today"],
            }
            for row in reversed(rows)
        ]

    def get_report_data(self, days: int = 30) -> dict[str, object]:
        """Get summary report data for the last N days.

        Args:
            days: Number of days to include in report.

        Returns:
            Dict with summary statistics.
        """
        trades = self.get_trade_history(days)
        total_trades = len(trades)
        filled = [t for t in trades if t.status == "filled"]
        resolved = [t for t in trades if t.status == "resolved"]

        # Simulated P&L from filled trades (edge-based estimate)
        simulated_pnl = Decimal("0")
        wins = 0
        losses = 0
        total_edge = Decimal("0")
        total_size = Decimal("0")

        for trade in filled:
            total_edge += abs(trade.edge)
            total_size += trade.size
            # In simulation, P&L is edge * size (simplified)
            pnl = trade.edge * trade.size
            simulated_pnl += pnl
            if pnl > Decimal("0"):
                wins += 1
            else:
                losses += 1

        # Actual P&L from resolved trades
        actual_pnl = Decimal("0")
        actual_wins = 0
        actual_losses = 0
        for trade in resolved:
            if trade.actual_pnl is not None:
                actual_pnl += trade.actual_pnl
                if trade.actual_pnl > Decimal("0"):
                    actual_wins += 1
                else:
                    actual_losses += 1

        avg_edge = total_edge / len(filled) if filled else Decimal("0")
        avg_size = total_size / len(filled) if filled else Decimal("0")
        win_rate = wins / len(filled) if filled else 0.0
        actual_win_rate = actual_wins / len(resolved) if resolved else 0.0

        return {
            "days": days,
            "total_trades": total_trades,
            "filled_trades": len(filled),
            "resolved_trades": len(resolved),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "actual_wins": actual_wins,
            "actual_losses": actual_losses,
            "actual_win_rate": actual_win_rate,
            "simulated_pnl": simulated_pnl,
            "actual_pnl": actual_pnl,
            "avg_edge": avg_edge,
            "avg_size": avg_size,
        }

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
            metric: Metric type (e.g., "temperature_high").
            threshold: Threshold value for the event.
            comparison: Comparison type ("above", "below", "between").

        Returns:
            True if cached successfully.
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(
                """INSERT OR REPLACE INTO markets
                   (market_id, location, lat, lon, event_date, metric,
                    threshold, comparison, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market_id,
                    location,
                    lat,
                    lon,
                    event_date.isoformat(),
                    metric,
                    threshold,
                    comparison,
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("market_cache_failed", market_id=market_id, error=str(e))
            return False

    def get_market_metadata(self, market_id: str) -> dict[str, object] | None:
        """Retrieve cached market metadata.

        Args:
            market_id: Market ID to look up.

        Returns:
            Dict with market metadata or None if not found.
        """
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM markets WHERE market_id = ?", (market_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "market_id": row["market_id"],
            "location": row["location"],
            "lat": row["lat"],
            "lon": row["lon"],
            "event_date": date.fromisoformat(str(row["event_date"])),
            "metric": row["metric"],
            "threshold": row["threshold"],
            "comparison": row["comparison"],
        }

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
