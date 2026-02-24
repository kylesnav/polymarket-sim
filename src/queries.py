"""Pure SQL query functions for the trade journal.

All database queries are pure functions that take a sqlite3.Connection
and return data. No connection management, no side effects.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import structlog

from src.models import Trade, WeatherEvent

logger = structlog.get_logger()


def insert_trade(
    conn: sqlite3.Connection,
    trade: Trade,
    market_context: dict[str, object] | None = None,
) -> bool:
    """Insert a trade record into the database.

    Args:
        conn: SQLite database connection.
        trade: Trade record to insert.
        market_context: Optional market metadata to store alongside.

    Returns:
        True if inserted successfully, False on error.
    """
    ctx = market_context or {}
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO trades
               (trade_id, market_id, side, price, size,
                noaa_probability, edge, timestamp, status,
                question, location, event_date_ctx, metric, threshold, comparison,
                noaa_forecast_high, noaa_forecast_low, noaa_forecast_narrative,
                event_id, bucket_index, token_id, outcome_label,
                fill_price, book_depth, resolution_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                str(ctx.get("question", "")),
                str(ctx.get("location", "")),
                str(ctx.get("event_date", "")),
                str(ctx.get("metric", "")),
                float(ctx.get("threshold", 0)),  # type: ignore[arg-type]
                str(ctx.get("comparison", "")),
                ctx.get("noaa_forecast_high"),
                ctx.get("noaa_forecast_low"),
                str(ctx.get("noaa_forecast_narrative", "")),
                trade.event_id,
                trade.bucket_index,
                trade.token_id,
                trade.outcome_label,
                str(trade.fill_price) if trade.fill_price is not None else None,
                (str(trade.book_depth_at_signal)
                 if trade.book_depth_at_signal is not None else None),
                trade.resolution_source,
            ),
        )
        conn.commit()
        logger.info("trade_logged", trade_id=trade.trade_id, market_id=trade.market_id)
        return True
    except sqlite3.Error as e:
        logger.error("trade_log_failed", trade_id=trade.trade_id, error=str(e))
        return False


def has_open_trade(conn: sqlite3.Connection, market_id: str) -> bool:
    """Check if a market has an open (pending or filled) trade.

    Args:
        conn: SQLite database connection.
        market_id: Market ID to check.

    Returns:
        True if an open trade exists.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM trades WHERE market_id = ? AND status IN ('pending', 'filled') LIMIT 1",
        (market_id,),
    )
    return cursor.fetchone() is not None


def get_open_position_size(conn: sqlite3.Connection, market_id: str) -> Decimal:
    """Get total size of open trades for a market.

    Args:
        conn: SQLite database connection.
        market_id: Market ID to check.

    Returns:
        Total position size, or zero if no open trades.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COALESCE(SUM(CAST(size AS REAL)), 0) FROM trades "
        "WHERE market_id = ? AND status IN ('pending', 'filled')",
        (market_id,),
    )
    return Decimal(str(cursor.fetchone()[0]))


def update_trade_status(
    conn: sqlite3.Connection, trade_id: str, status: str
) -> bool:
    """Update the status of a trade.

    Args:
        conn: SQLite database connection.
        trade_id: ID of the trade to update.
        status: New status value.

    Returns:
        True if updated successfully.
    """
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE trades SET status = ? WHERE trade_id = ?",
            (status, trade_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error("trade_update_failed", trade_id=trade_id, error=str(e))
        return False


def update_trade_resolution(
    conn: sqlite3.Connection,
    trade_id: str,
    outcome: str,
    actual_pnl: Decimal,
    actual_value: float | None = None,
    actual_value_unit: str = "",
) -> bool:
    """Update a trade with resolution outcome and actual P&L.

    Args:
        conn: SQLite database connection.
        trade_id: ID of the trade to resolve.
        outcome: "won" or "lost".
        actual_pnl: Actual profit/loss from the trade.
        actual_value: The actual observed weather value.
        actual_value_unit: Unit for the actual value.

    Returns:
        True if updated successfully.
    """
    try:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE trades
               SET status = ?, outcome = ?, actual_pnl = ?,
                   actual_value = ?, actual_value_unit = ?
               WHERE trade_id = ?""",
            ("resolved", outcome, str(actual_pnl), actual_value, actual_value_unit, trade_id),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error("trade_resolution_failed", trade_id=trade_id, error=str(e))
        return False


def get_unresolved_trades(conn: sqlite3.Connection) -> list[Trade]:
    """Get all filled trades that have not been resolved.

    Args:
        conn: SQLite database connection.

    Returns:
        List of unresolved Trade records.
    """
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM trades
           WHERE status = 'filled'
           ORDER BY timestamp ASC"""
    )
    return [_row_to_trade(row) for row in cursor.fetchall()]


def get_daily_pnl(conn: sqlite3.Connection, target_date: date) -> Decimal:
    """Get the total P&L for a specific date.

    Args:
        conn: SQLite database connection.
        target_date: Date to query.

    Returns:
        Daily P&L as Decimal.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT daily_pnl FROM daily_snapshots WHERE snapshot_date = ?",
        (target_date.isoformat(),),
    )
    row = cursor.fetchone()
    if row is not None:
        return Decimal(str(row["daily_pnl"]))
    return Decimal("0")


def save_daily_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: date,
    cash: Decimal,
    total_value: Decimal,
    daily_pnl: Decimal,
    open_positions: int,
    trades_today: int,
) -> None:
    """Save or update a daily portfolio snapshot.

    Args:
        conn: SQLite database connection.
        snapshot_date: Date of the snapshot.
        cash: Cash balance.
        total_value: Total portfolio value.
        daily_pnl: P&L for the day.
        open_positions: Number of open positions.
        trades_today: Number of trades executed today.
    """
    try:
        cursor = conn.cursor()
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
        conn.commit()
    except sqlite3.Error as e:
        logger.error("snapshot_save_failed", error=str(e))


def get_trade_history(conn: sqlite3.Connection, days: int = 30) -> list[Trade]:
    """Get trade history for the last N days.

    Args:
        conn: SQLite database connection.
        days: Number of days of history to retrieve.

    Returns:
        List of Trade records.
    """
    now = datetime.now(tz=UTC).isoformat()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM trades
           WHERE timestamp >= date(?, ?)
           ORDER BY timestamp DESC""",
        (now, f"-{days} days"),
    )
    return [_row_to_trade(row) for row in cursor.fetchall()]


def get_trades_with_context(
    conn: sqlite3.Connection,
    days: int = 90,
    status: str | None = None,
    outcome: str | None = None,
) -> list[dict[str, object]]:
    """Get trades with market context and lifecycle state.

    Args:
        conn: SQLite database connection.
        days: Number of days of history.
        status: Optional status filter.
        outcome: Optional outcome filter.

    Returns:
        List of enriched trade dicts with lifecycle field.
    """
    now = datetime.now(tz=UTC).isoformat()
    cursor = conn.cursor()

    query = """SELECT * FROM trades
               WHERE timestamp >= date(?, ?)"""
    params: list[object] = [now, f"-{days} days"]

    if status:
        query += " AND status = ?"
        params.append(status)
    if outcome:
        query += " AND outcome = ?"
        params.append(outcome)

    query += " ORDER BY timestamp DESC"
    cursor.execute(query, params)

    today = date.today()
    return [_row_to_context_dict(row, today) for row in cursor.fetchall()]


def get_trade_detail(
    conn: sqlite3.Connection, trade_id: str
) -> dict[str, object] | None:
    """Get a single trade with full context.

    Args:
        conn: SQLite database connection.
        trade_id: Trade ID to look up.

    Returns:
        Enriched trade dict or None if not found.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_context_dict(row, date.today())


def get_lifecycle_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Get counts of trades by lifecycle state.

    Args:
        conn: SQLite database connection.

    Returns:
        Dict with open, ready, resolved, total counts.
    """
    today = date.today().isoformat()
    cursor = conn.cursor()

    cursor.execute(
        """SELECT
               SUM(CASE
                   WHEN status = 'filled'
                       AND event_date_ctx != ''
                       AND event_date_ctx >= ?
                   THEN 1 ELSE 0
               END) AS open_bets,
               SUM(CASE
                   WHEN status = 'filled'
                       AND event_date_ctx != ''
                       AND event_date_ctx < ?
                   THEN 1 ELSE 0
               END) AS ready,
               SUM(CASE
                   WHEN status = 'filled'
                       AND event_date_ctx = ''
                   THEN 1 ELSE 0
               END) AS unknown,
               SUM(CASE
                   WHEN status = 'resolved'
                   THEN 1 ELSE 0
               END) AS resolved,
               COUNT(*) AS total
           FROM trades
           WHERE status IN ('filled', 'resolved')""",
        (today, today),
    )
    row = cursor.fetchone()
    if row is None:
        return {"open": 0, "ready": 0, "resolved": 0, "total": 0}

    return {
        "open": int(row["open_bets"] or 0) + int(row["unknown"] or 0),
        "ready": int(row["ready"] or 0),
        "resolved": int(row["resolved"] or 0),
        "total": int(row["total"] or 0),
    }


def get_portfolio_summary(
    conn: sqlite3.Connection, starting_bankroll: Decimal
) -> dict[str, object]:
    """Compute portfolio state from trade history.

    Args:
        conn: SQLite database connection.
        starting_bankroll: The starting bankroll amount.

    Returns:
        Dict with cash, exposure, total_value, actual_pnl, and lifecycle counts.
    """
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COALESCE(SUM(CAST(size AS REAL)), 0) FROM trades WHERE status = 'filled'"
    )
    exposure = Decimal(str(cursor.fetchone()[0]))

    cursor.execute(
        "SELECT COALESCE(SUM(CAST(actual_pnl AS REAL)), 0) "
        "FROM trades WHERE status = 'resolved'"
    )
    realized_pnl = Decimal(str(cursor.fetchone()[0]))

    cash = starting_bankroll - exposure + realized_pnl
    total_value = cash + exposure

    lifecycle = get_lifecycle_counts(conn)

    return {
        "starting_bankroll": starting_bankroll,
        "cash": cash,
        "exposure": exposure,
        "total_value": total_value,
        "actual_pnl": realized_pnl,
        **lifecycle,
    }


def backfill_trade_context(conn: sqlite3.Connection) -> None:
    """Backfill context columns from markets cache for existing trades.

    Args:
        conn: SQLite database connection.
    """
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE trades SET
               question = COALESCE(
                   (SELECT 'Will ' || m.location || ' ' ||
                    REPLACE(REPLACE(REPLACE(REPLACE(m.metric,
                        'temperature_high', 'high temp'),
                        'temperature_low', 'low temp'),
                        'precipitation', 'precipitation'),
                        'snowfall', 'snowfall') ||
                    ' be ' || m.comparison || ' ' ||
                    CAST(m.threshold AS TEXT) || ' on ' || m.event_date
                    FROM markets m WHERE m.market_id = trades.market_id),
                   question),
               location = COALESCE(
                   (SELECT m.location FROM markets m WHERE m.market_id = trades.market_id),
                   location),
               event_date_ctx = COALESCE(
                   (SELECT m.event_date FROM markets m WHERE m.market_id = trades.market_id),
                   event_date_ctx),
               metric = COALESCE(
                   (SELECT m.metric FROM markets m WHERE m.market_id = trades.market_id),
                   metric),
               threshold = COALESCE(
                   (SELECT m.threshold FROM markets m WHERE m.market_id = trades.market_id),
                   threshold),
               comparison = COALESCE(
                   (SELECT m.comparison FROM markets m WHERE m.market_id = trades.market_id),
                   comparison)
           WHERE location = '' AND EXISTS (
               SELECT 1 FROM markets m WHERE m.market_id = trades.market_id
           )"""
    )
    if cursor.rowcount > 0:
        logger.info("backfilled_trade_context", count=cursor.rowcount)
    conn.commit()


def cache_market(
    conn: sqlite3.Connection,
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
        conn: SQLite database connection.
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
    try:
        cursor = conn.cursor()
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
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error("market_cache_failed", market_id=market_id, error=str(e))
        return False


def get_market_metadata(
    conn: sqlite3.Connection, market_id: str
) -> dict[str, object] | None:
    """Retrieve cached market metadata.

    Args:
        conn: SQLite database connection.
        market_id: Market ID to look up.

    Returns:
        Dict with market metadata or None if not found.
    """
    cursor = conn.cursor()
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


def get_snapshots(conn: sqlite3.Connection, days: int = 60) -> list[dict[str, object]]:
    """Get daily snapshots for the last N days.

    Args:
        conn: SQLite database connection.
        days: Number of days of snapshots to retrieve.

    Returns:
        List of snapshot dicts ordered by date ascending.
    """
    cursor = conn.cursor()
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


def get_open_positions_with_pnl(conn: sqlite3.Connection) -> dict[str, Any]:
    """Get all open positions with per-trade and aggregate P&L estimates.

    For each filled trade, computes:
    - max_profit: payout if contract wins (pays $1)
    - max_loss: total loss of stake (-size)
    - expected_pnl: probability-weighted estimate using NOAA probability
    - expected_return: expected_pnl / size

    Args:
        conn: SQLite database connection.

    Returns:
        Dict with "positions" list and "summary" aggregates.
    """
    today = date.today()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM trades WHERE status = 'filled' ORDER BY timestamp DESC"""
    )

    positions: list[dict[str, Any]] = []
    total_exposure = Decimal("0")
    total_max_profit = Decimal("0")
    total_max_loss = Decimal("0")
    total_expected_pnl = Decimal("0")

    for row in cursor.fetchall():
        side = str(row["side"])
        price = Decimal(str(row["price"]))
        size = Decimal(str(row["size"]))
        raw_noaa = row["noaa_probability"]
        noaa_prob = (
            Decimal(str(raw_noaa))
            if raw_noaa is not None
            else Decimal("0.5")
        )
        edge = Decimal(str(row["edge"]))

        effective_price = price if side == "YES" else (Decimal("1") - price)
        if effective_price > 0:
            max_profit = size * (Decimal("1") - effective_price) / effective_price
        else:
            max_profit = Decimal("0")
        max_loss = -size

        # Win probability depends on trade side: YES wins when event occurs,
        # NO wins when event does NOT occur.
        win_prob = noaa_prob if side == "YES" else (Decimal("1") - noaa_prob)
        expected_pnl = win_prob * max_profit + (Decimal("1") - win_prob) * max_loss
        expected_return = expected_pnl / size if size > 0 else Decimal("0")

        event_date_str = str(row["event_date_ctx"]) if row["event_date_ctx"] else ""
        days_until: int | None = None
        if event_date_str:
            try:
                event_dt = date.fromisoformat(event_date_str)
                days_until = (event_dt - today).days
            except ValueError:
                pass

        positions.append({
            "trade_id": str(row["trade_id"]),
            "market_id": str(row["market_id"]),
            "question": str(row["question"]) if row["question"] else "",
            "location": str(row["location"]) if row["location"] else "",
            "side": side,
            "entry_price": price,
            "size": size,
            "noaa_probability": noaa_prob,
            "edge": edge,
            "max_profit": max_profit.quantize(Decimal("0.01")),
            "max_loss": max_loss.quantize(Decimal("0.01")),
            "expected_pnl": expected_pnl.quantize(Decimal("0.01")),
            "expected_return": expected_return.quantize(Decimal("0.0001")),
            "event_date": event_date_str,
            "days_until_event": days_until,
            "metric": str(row["metric"]) if row["metric"] else "",
            "threshold": float(row["threshold"]) if row["threshold"] else 0.0,
            "comparison": str(row["comparison"]) if row["comparison"] else "",
            "timestamp": str(row["timestamp"]),
        })

        total_exposure += size
        total_max_profit += max_profit
        total_max_loss += max_loss
        total_expected_pnl += expected_pnl

    total_expected_return = (
        total_expected_pnl / total_exposure
        if total_exposure > 0
        else Decimal("0")
    )

    return {
        "positions": positions,
        "summary": {
            "position_count": len(positions),
            "total_exposure": total_exposure.quantize(Decimal("0.01")),
            "total_max_profit": total_max_profit.quantize(Decimal("0.01")),
            "total_max_loss": total_max_loss.quantize(Decimal("0.01")),
            "total_expected_pnl": total_expected_pnl.quantize(Decimal("0.01")),
            "total_expected_return": total_expected_return.quantize(Decimal("0.0001")),
        },
    }


def get_report_data(conn: sqlite3.Connection, days: int = 30) -> dict[str, Any]:
    """Get summary report data for the last N days.

    Args:
        conn: SQLite database connection.
        days: Number of days to include in report.

    Returns:
        Dict with summary statistics.
    """
    trades = get_trade_history(conn, days)
    total_trades = len(trades)
    filled = [t for t in trades if t.status == "filled"]
    resolved = [t for t in trades if t.status == "resolved"]

    simulated_pnl = Decimal("0")
    wins = 0
    losses = 0
    total_edge = Decimal("0")
    total_size = Decimal("0")

    for trade in filled:
        total_edge += abs(trade.edge)
        total_size += trade.size
        pnl = trade.edge * trade.size
        simulated_pnl += pnl
        if pnl > Decimal("0"):
            wins += 1
        else:
            losses += 1

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


def cache_event(conn: sqlite3.Connection, event: WeatherEvent) -> bool:
    """Cache a multi-outcome weather event's metadata.

    Args:
        conn: SQLite database connection.
        event: WeatherEvent to cache.

    Returns:
        True if cached successfully.
    """
    import json

    try:
        cursor = conn.cursor()
        bucket_labels = json.dumps([b.outcome_label for b in event.buckets])
        cursor.execute(
            """INSERT OR REPLACE INTO events
               (event_id, question, location, lat, lon, event_date,
                metric, bucket_count, bucket_labels, cached_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.question,
                event.location,
                event.lat,
                event.lon,
                event.event_date.isoformat(),
                event.metric,
                len(event.buckets),
                bucket_labels,
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error("event_cache_failed", event_id=event.event_id, error=str(e))
        return False


def get_event_metadata(
    conn: sqlite3.Connection, event_id: str
) -> dict[str, object] | None:
    """Retrieve cached event metadata.

    Args:
        conn: SQLite database connection.
        event_id: Event ID to look up.

    Returns:
        Dict with event metadata or None if not found.
    """
    import json

    cursor = conn.cursor()
    cursor.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "event_id": row["event_id"],
        "question": row["question"],
        "location": row["location"],
        "lat": row["lat"],
        "lon": row["lon"],
        "event_date": date.fromisoformat(str(row["event_date"])),
        "metric": row["metric"],
        "bucket_count": row["bucket_count"],
        "bucket_labels": json.loads(str(row["bucket_labels"])),
    }


def get_trades_by_event(
    conn: sqlite3.Connection, event_id: str
) -> list[dict[str, object]]:
    """Get all trades for a specific event.

    Args:
        conn: SQLite database connection.
        event_id: Event ID to query.

    Returns:
        List of enriched trade dicts for the event.
    """
    cursor = conn.cursor()
    cursor.execute(
        """SELECT * FROM trades
           WHERE event_id = ?
           ORDER BY bucket_index ASC, timestamp DESC""",
        (event_id,),
    )
    today = date.today()
    return [_row_to_context_dict(row, today) for row in cursor.fetchall()]


def _row_to_trade(row: sqlite3.Row) -> Trade:
    """Convert a database row to a Trade model.

    Args:
        row: SQLite row from the trades table.

    Returns:
        Trade model instance.
    """
    return Trade(
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
        event_id=str(row["event_id"]) if row["event_id"] else "",
        bucket_index=int(row["bucket_index"]) if row["bucket_index"] is not None else -1,
        token_id=str(row["token_id"]) if row["token_id"] else "",
        outcome_label=str(row["outcome_label"]) if row["outcome_label"] else "",
        fill_price=Decimal(str(row["fill_price"])) if row["fill_price"] else None,
        book_depth_at_signal=Decimal(str(row["book_depth"])) if row["book_depth"] else None,
        resolution_source=str(row["resolution_source"]) if row["resolution_source"] else "",
    )


def _row_to_context_dict(
    row: sqlite3.Row, today: date
) -> dict[str, object]:
    """Convert a trades table row to an enriched dict with lifecycle.

    Args:
        row: SQLite row from the trades table.
        today: Current date for lifecycle computation.

    Returns:
        Dict with all trade fields, market context, and lifecycle state.
    """
    status = str(row["status"])
    event_date_str = str(row["event_date_ctx"]) if row["event_date_ctx"] else ""

    if status == "resolved":
        lifecycle = "resolved"
    elif status in ("filled", "pending") and event_date_str:
        event_dt = date.fromisoformat(event_date_str)
        lifecycle = "open" if event_dt >= today else "ready"
    else:
        lifecycle = "open"

    days_until: int | None = None
    if event_date_str:
        try:
            event_dt = date.fromisoformat(event_date_str)
            days_until = (event_dt - today).days
        except ValueError:
            pass

    side = str(row["side"])
    price = Decimal(str(row["price"]))
    size = Decimal(str(row["size"]))
    effective_price = price if side == "YES" else (Decimal("1") - price)
    potential_payout = size * (Decimal("1") - effective_price) / effective_price

    return {
        "trade_id": str(row["trade_id"]),
        "market_id": str(row["market_id"]),
        "side": str(row["side"]),
        "price": price,
        "size": size,
        "noaa_probability": Decimal(str(row["noaa_probability"])),
        "edge": Decimal(str(row["edge"])),
        "timestamp": str(row["timestamp"]),
        "status": status,
        "outcome": str(row["outcome"]) if row["outcome"] else None,
        "actual_pnl": Decimal(str(row["actual_pnl"])) if row["actual_pnl"] else None,
        "question": str(row["question"]) if row["question"] else "",
        "location": str(row["location"]) if row["location"] else "",
        "event_date": event_date_str,
        "metric": str(row["metric"]) if row["metric"] else "",
        "threshold": float(row["threshold"]) if row["threshold"] else 0.0,
        "comparison": str(row["comparison"]) if row["comparison"] else "",
        "lifecycle": lifecycle,
        "days_until_event": days_until,
        "potential_payout": potential_payout,
        "actual_value": (
            float(row["actual_value"])
            if row["actual_value"] is not None else None
        ),
        "actual_value_unit": (
            str(row["actual_value_unit"])
            if row["actual_value_unit"] else ""
        ),
        "noaa_forecast_high": (
            float(row["noaa_forecast_high"])
            if row["noaa_forecast_high"] is not None else None
        ),
        "noaa_forecast_low": (
            float(row["noaa_forecast_low"])
            if row["noaa_forecast_low"] is not None else None
        ),
        "noaa_forecast_narrative": (
            str(row["noaa_forecast_narrative"])
            if row["noaa_forecast_narrative"] else ""
        ),
    }
