"""Tests for database schema and migrations."""

from __future__ import annotations

import sqlite3

from src.schema import (
    create_tables,
    ensure_context_columns,
    get_schema_version,
    initialize_schema,
    run_migrations,
)


def _in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class TestCreateTables:
    """Tests for create_tables."""

    def test_creates_all_tables(self) -> None:
        conn = _in_memory_conn()
        create_tables(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "trades" in tables
        assert "positions" in tables
        assert "daily_snapshots" in tables
        assert "markets" in tables
        assert "schema_version" in tables
        conn.close()

    def test_idempotent(self) -> None:
        conn = _in_memory_conn()
        create_tables(conn)
        create_tables(conn)  # Should not raise
        conn.close()


class TestEnsureContextColumns:
    """Tests for ensure_context_columns."""

    def test_adds_context_columns(self) -> None:
        conn = _in_memory_conn()
        create_tables(conn)
        ensure_context_columns(conn)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(trades)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "question" in cols
        assert "location" in cols
        assert "event_date_ctx" in cols
        assert "noaa_forecast_high" in cols
        conn.close()

    def test_idempotent(self) -> None:
        conn = _in_memory_conn()
        create_tables(conn)
        ensure_context_columns(conn)
        ensure_context_columns(conn)  # Should not raise
        conn.close()


class TestSchemaVersion:
    """Tests for schema versioning."""

    def test_returns_zero_before_migrations(self) -> None:
        conn = _in_memory_conn()
        assert get_schema_version(conn) == 0
        conn.close()

    def test_returns_version_after_migration(self) -> None:
        conn = _in_memory_conn()
        create_tables(conn)
        run_migrations(conn)
        assert get_schema_version(conn) >= 1
        conn.close()


class TestInitializeSchema:
    """Tests for initialize_schema."""

    def test_full_initialization(self) -> None:
        conn = _in_memory_conn()
        initialize_schema(conn)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert "trades" in tables
        assert "schema_version" in tables
        assert get_schema_version(conn) >= 1
        conn.close()

    def test_idempotent(self) -> None:
        conn = _in_memory_conn()
        initialize_schema(conn)
        initialize_schema(conn)  # Should not raise
        conn.close()
