"""Database schema definitions and migration support.

All CREATE TABLE statements and schema versioning live here,
keeping journal.py focused on connection management and business logic.
"""

from __future__ import annotations

import sqlite3

import structlog

logger = structlog.get_logger()

SCHEMA_VERSION = 3

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

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    location TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    event_date TEXT NOT NULL,
    metric TEXT NOT NULL,
    bucket_count INTEGER NOT NULL,
    bucket_labels TEXT NOT NULL,
    cached_at TEXT NOT NULL
)
"""

CREATE_SCHEMA_VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

# Multi-outcome columns added to trades in schema v3.
MULTI_OUTCOME_COLUMNS = [
    ("event_id", "TEXT DEFAULT ''"),
    ("bucket_index", "INTEGER DEFAULT -1"),
    ("token_id", "TEXT DEFAULT ''"),
    ("outcome_label", "TEXT DEFAULT ''"),
    ("fill_price", "TEXT DEFAULT NULL"),
    ("book_depth", "TEXT DEFAULT NULL"),
    ("resolution_source", "TEXT DEFAULT ''"),
]

# Context columns added to the trades table for human-readable display.
CONTEXT_COLUMNS = [
    ("question", "TEXT DEFAULT ''"),
    ("location", "TEXT DEFAULT ''"),
    ("event_date_ctx", "TEXT DEFAULT ''"),
    ("metric", "TEXT DEFAULT ''"),
    ("threshold", "REAL DEFAULT 0"),
    ("comparison", "TEXT DEFAULT ''"),
    ("actual_value", "REAL DEFAULT NULL"),
    ("actual_value_unit", "TEXT DEFAULT ''"),
    ("noaa_forecast_high", "REAL DEFAULT NULL"),
    ("noaa_forecast_low", "REAL DEFAULT NULL"),
    ("noaa_forecast_narrative", "TEXT DEFAULT ''"),
]


def create_tables(conn: sqlite3.Connection) -> None:
    """Create all database tables if they don't exist.

    Args:
        conn: SQLite database connection.
    """
    cursor = conn.cursor()
    cursor.execute(CREATE_TRADES_TABLE)
    cursor.execute(CREATE_POSITIONS_TABLE)
    cursor.execute(CREATE_DAILY_SNAPSHOTS_TABLE)
    cursor.execute(CREATE_MARKETS_TABLE)
    cursor.execute(CREATE_EVENTS_TABLE)
    cursor.execute(CREATE_SCHEMA_VERSION_TABLE)
    conn.commit()


def ensure_context_columns(conn: sqlite3.Connection) -> None:
    """Add context columns to trades table if they don't exist.

    Args:
        conn: SQLite database connection.
    """
    cursor = conn.cursor()
    for col_name, col_type in CONTEXT_COLUMNS:
        try:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


def ensure_multi_outcome_columns(conn: sqlite3.Connection) -> None:
    """Add multi-outcome columns to trades table if they don't exist.

    Args:
        conn: SQLite database connection.
    """
    cursor = conn.cursor()
    for col_name, col_type in MULTI_OUTCOME_COLUMNS:
        try:
            cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version from the database.

    Args:
        conn: SQLite database connection.

    Returns:
        Current schema version, or 0 if no version table exists.
    """
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT MAX(version) FROM schema_version")
        row = cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run any pending schema migrations.

    Args:
        conn: SQLite database connection.
    """
    current = get_schema_version(conn)

    migrations: list[tuple[int, str, str]] = [
        (1, "Initial schema", ""),  # Handled by create_tables
        (2, "Add context columns", ""),  # Handled by ensure_context_columns
        (3, "Add multi-outcome columns and events table", ""),
    ]

    for version, description, _sql in migrations:
        if version > current:
            logger.info(
                "applying_migration",
                version=version,
                description=description,
            )
            # Migrations 1-2 are structural and handled by other functions
            from datetime import UTC, datetime

            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now(tz=UTC).isoformat()),
            )
            conn.commit()


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Full schema initialization: create tables, run migrations, add columns.

    Args:
        conn: SQLite database connection.
    """
    create_tables(conn)
    ensure_context_columns(conn)
    ensure_multi_outcome_columns(conn)
    run_migrations(conn)
