"""Tests for journal enrichment, lifecycle, and portfolio computation."""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from src.journal import Journal
from src.models import Trade


def _make_journal() -> Journal:
    """Create an in-memory-style journal using a temp file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = Path(tmp.name)
    return Journal(db_path=db_path)


def _make_trade(
    *,
    trade_id: str = "t001",
    market_id: str = "mkt001",
    side: str = "YES",
    price: str = "0.40",
    size: str = "25.00",
    noaa_probability: str = "0.80",
    edge: str = "0.40",
    status: str = "pending",
) -> Trade:
    return Trade(
        trade_id=trade_id,
        market_id=market_id,
        side=side,  # type: ignore[arg-type]
        price=Decimal(price),
        size=Decimal(size),
        noaa_probability=Decimal(noaa_probability),
        edge=Decimal(edge),
        timestamp=datetime.now(tz=UTC),
        status=status,  # type: ignore[arg-type]
    )


class TestEnsureColumns:
    """Tests for _ensure_context_columns idempotency."""

    def test_columns_added_on_init(self) -> None:
        """Context columns exist after journal init."""
        j = _make_journal()
        cursor = j._conn.cursor()
        cursor.execute("PRAGMA table_info(trades)")
        col_names = {row[1] for row in cursor.fetchall()}
        j.close()

        assert "question" in col_names
        assert "location" in col_names
        assert "event_date_ctx" in col_names
        assert "metric" in col_names
        assert "threshold" in col_names
        assert "comparison" in col_names
        assert "actual_value" in col_names
        assert "actual_value_unit" in col_names
        assert "noaa_forecast_high" in col_names
        assert "noaa_forecast_low" in col_names
        assert "noaa_forecast_narrative" in col_names

    def test_ensure_columns_is_idempotent(self) -> None:
        """Running _ensure_context_columns twice does not raise."""
        j = _make_journal()
        j._ensure_context_columns()  # second call — should not error
        j.close()


class TestLogTradeWithContext:
    """Tests for log_trade with market_context kwarg."""

    def test_log_trade_stores_context(self) -> None:
        """Context columns are written when market_context is provided."""
        j = _make_journal()
        trade = _make_trade()
        context = {
            "question": "Will Seattle high temp be above 35F?",
            "location": "Seattle, WA",
            "event_date": "2026-02-25",
            "metric": "temperature_high",
            "threshold": 35.0,
            "comparison": "above",
        }
        ok = j.log_trade(trade, market_context=context)
        assert ok is True

        cursor = j._conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE trade_id = ?", ("t001",))
        row = cursor.fetchone()
        j.close()

        assert row["question"] == "Will Seattle high temp be above 35F?"
        assert row["location"] == "Seattle, WA"
        assert row["event_date_ctx"] == "2026-02-25"
        assert row["metric"] == "temperature_high"
        assert float(row["threshold"]) == 35.0
        assert row["comparison"] == "above"

    def test_log_trade_without_context(self) -> None:
        """Trade logged without context has empty defaults."""
        j = _make_journal()
        trade = _make_trade()
        ok = j.log_trade(trade)
        assert ok is True

        cursor = j._conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE trade_id = ?", ("t001",))
        row = cursor.fetchone()
        j.close()

        assert row["question"] == ""
        assert row["location"] == ""
        assert row["event_date_ctx"] == ""


class TestBackfillTradeContext:
    """Tests for _backfill_trade_context."""

    def test_backfill_populates_from_markets_cache(self) -> None:
        """Trades with empty location get backfilled from markets table."""
        j = _make_journal()
        # Log a trade without context
        trade = _make_trade()
        j.log_trade(trade)

        # Cache market metadata
        j.cache_market(
            market_id="mkt001",
            location="Portland, OR",
            lat=45.5,
            lon=-122.6,
            event_date=date(2026, 2, 25),
            metric="temperature_low",
            threshold=30.0,
            comparison="below",
        )

        # Run backfill
        j.backfill_trade_context()

        cursor = j._conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE trade_id = ?", ("t001",))
        row = cursor.fetchone()
        j.close()

        assert row["location"] == "Portland, OR"
        assert row["event_date_ctx"] == "2026-02-25"
        assert row["metric"] == "temperature_low"

    def test_backfill_skips_already_populated(self) -> None:
        """Trades with existing context are not overwritten."""
        j = _make_journal()
        trade = _make_trade()
        context = {
            "question": "Original question",
            "location": "Denver, CO",
            "event_date": "2026-03-01",
            "metric": "snowfall",
            "threshold": 2.0,
            "comparison": "above",
        }
        j.log_trade(trade, market_context=context)

        # Cache different metadata
        j.cache_market(
            market_id="mkt001",
            location="Different City",
            lat=40.0,
            lon=-105.0,
            event_date=date(2026, 3, 5),
            metric="precipitation",
            threshold=0.5,
            comparison="above",
        )

        j.backfill_trade_context()

        cursor = j._conn.cursor()
        cursor.execute("SELECT * FROM trades WHERE trade_id = ?", ("t001",))
        row = cursor.fetchone()
        j.close()

        assert row["location"] == "Denver, CO"  # Not overwritten


class TestGetTradesWithContext:
    """Tests for get_trades_with_context with lifecycle computation."""

    def test_lifecycle_open_for_future_event(self) -> None:
        """Filled trade with future event_date has lifecycle 'open'."""
        j = _make_journal()
        future_date = (date.today() + timedelta(days=3)).isoformat()
        trade = _make_trade(status="pending")
        context = {
            "question": "Will NYC high temp be above 50F?",
            "location": "New York, NY",
            "event_date": future_date,
            "metric": "temperature_high",
            "threshold": 50.0,
            "comparison": "above",
        }
        j.log_trade(trade, market_context=context)
        j.update_trade_status("t001", "filled")

        trades = j.get_trades_with_context(days=90)
        j.close()

        assert len(trades) == 1
        assert trades[0]["lifecycle"] == "open"
        assert trades[0]["days_until_event"] is not None
        assert trades[0]["days_until_event"] > 0  # type: ignore[operator]

    def test_lifecycle_ready_for_past_event(self) -> None:
        """Filled trade with past event_date has lifecycle 'ready'."""
        j = _make_journal()
        past_date = (date.today() - timedelta(days=2)).isoformat()
        trade = _make_trade(trade_id="t002", status="pending")
        context = {
            "question": "Will Chicago low temp be below 20F?",
            "location": "Chicago, IL",
            "event_date": past_date,
            "metric": "temperature_low",
            "threshold": 20.0,
            "comparison": "below",
        }
        j.log_trade(trade, market_context=context)
        j.update_trade_status("t002", "filled")

        trades = j.get_trades_with_context(days=90)
        j.close()

        assert len(trades) == 1
        assert trades[0]["lifecycle"] == "ready"
        assert trades[0]["days_until_event"] < 0  # type: ignore[operator]

    def test_lifecycle_resolved(self) -> None:
        """Resolved trade has lifecycle 'resolved'."""
        j = _make_journal()
        trade = _make_trade(trade_id="t003", status="pending")
        context = {
            "question": "Will Miami precipitation be above 0.5 in?",
            "location": "Miami, FL",
            "event_date": (date.today() - timedelta(days=5)).isoformat(),
            "metric": "precipitation",
            "threshold": 0.5,
            "comparison": "above",
        }
        j.log_trade(trade, market_context=context)
        j.update_trade_status("t003", "filled")
        j.update_trade_resolution("t003", "won", Decimal("15.00"))

        trades = j.get_trades_with_context(days=90)
        j.close()

        assert len(trades) == 1
        assert trades[0]["lifecycle"] == "resolved"
        assert trades[0]["outcome"] == "won"
        assert trades[0]["actual_pnl"] == Decimal("15.00")

    def test_status_filter(self) -> None:
        """Status filter limits returned trades."""
        j = _make_journal()
        t1 = _make_trade(trade_id="t010", status="pending")
        t2 = _make_trade(trade_id="t011", status="pending")
        j.log_trade(t1)
        j.log_trade(t2)
        j.update_trade_status("t010", "filled")
        j.update_trade_status("t011", "resolved")

        filled = j.get_trades_with_context(days=90, status="filled")
        resolved = j.get_trades_with_context(days=90, status="resolved")
        j.close()

        assert len(filled) == 1
        assert filled[0]["trade_id"] == "t010"
        assert len(resolved) == 1
        assert resolved[0]["trade_id"] == "t011"


class TestGetTradeDetail:
    """Tests for get_trade_detail."""

    def test_returns_enriched_trade(self) -> None:
        """Single trade lookup returns full context."""
        j = _make_journal()
        trade = _make_trade(trade_id="t100")
        context = {
            "question": "Will Seattle high temp be above 60F?",
            "location": "Seattle, WA",
            "event_date": "2026-02-28",
            "metric": "temperature_high",
            "threshold": 60.0,
            "comparison": "above",
        }
        j.log_trade(trade, market_context=context)

        detail = j.get_trade_detail("t100")
        j.close()

        assert detail is not None
        assert detail["question"] == "Will Seattle high temp be above 60F?"
        assert detail["location"] == "Seattle, WA"
        assert detail["potential_payout"] == Decimal("25.00") * (Decimal("1") - Decimal("0.40"))

    def test_returns_none_for_missing(self) -> None:
        """Missing trade returns None."""
        j = _make_journal()
        detail = j.get_trade_detail("nonexistent")
        j.close()
        assert detail is None


class TestGetLifecycleCounts:
    """Tests for get_lifecycle_counts."""

    def test_empty_journal(self) -> None:
        """Empty journal returns all zeros."""
        j = _make_journal()
        counts = j.get_lifecycle_counts()
        j.close()
        assert counts == {"open": 0, "ready": 0, "resolved": 0, "total": 0}

    def test_mixed_lifecycle(self) -> None:
        """Counts correctly categorize open, ready, and resolved trades."""
        j = _make_journal()
        future = (date.today() + timedelta(days=5)).isoformat()
        past = (date.today() - timedelta(days=2)).isoformat()

        # Open trade (future event)
        t1 = _make_trade(trade_id="lc01")
        j.log_trade(t1, market_context={"event_date": future, "location": "A"})
        j.update_trade_status("lc01", "filled")

        # Ready trade (past event)
        t2 = _make_trade(trade_id="lc02", market_id="mkt002")
        j.log_trade(t2, market_context={"event_date": past, "location": "B"})
        j.update_trade_status("lc02", "filled")

        # Resolved trade
        t3 = _make_trade(trade_id="lc03", market_id="mkt003")
        j.log_trade(t3, market_context={"event_date": past, "location": "C"})
        j.update_trade_status("lc03", "filled")
        j.update_trade_resolution("lc03", "won", Decimal("10"))

        counts = j.get_lifecycle_counts()
        j.close()

        assert counts["open"] == 1
        assert counts["ready"] == 1
        assert counts["resolved"] == 1
        assert counts["total"] == 3


class TestGetPortfolioSummary:
    """Tests for get_portfolio_summary."""

    def test_empty_portfolio(self) -> None:
        """Empty journal returns starting bankroll as cash."""
        j = _make_journal()
        summary = j.get_portfolio_summary(Decimal("500"))
        j.close()

        assert summary["cash"] == Decimal("500")
        assert summary["exposure"] == Decimal("0")
        assert summary["total_value"] == Decimal("500")
        assert summary["actual_pnl"] == Decimal("0")

    def test_portfolio_with_filled_trade(self) -> None:
        """Filled trade reduces cash and increases exposure."""
        j = _make_journal()
        trade = _make_trade(size="25.00")
        j.log_trade(trade)
        j.update_trade_status("t001", "filled")

        summary = j.get_portfolio_summary(Decimal("500"))
        j.close()

        assert summary["exposure"] == Decimal("25")
        assert summary["cash"] == Decimal("475")  # 500 - 25
        assert summary["total_value"] == Decimal("500")  # cash + exposure

    def test_portfolio_with_resolved_win(self) -> None:
        """Resolved winning trade adds pnl to cash."""
        j = _make_journal()
        trade = _make_trade(size="25.00", price="0.40")
        j.log_trade(trade)
        j.update_trade_status("t001", "filled")
        j.update_trade_resolution("t001", "won", Decimal("15.00"))

        summary = j.get_portfolio_summary(Decimal("500"))
        j.close()

        # Resolved: exposure=0, cash = 500 - 0 + 15 = 515
        assert summary["exposure"] == Decimal("0")
        assert summary["actual_pnl"] == Decimal("15")
        assert summary["cash"] == Decimal("515")

    def test_portfolio_with_resolved_loss(self) -> None:
        """Resolved losing trade subtracts from cash."""
        j = _make_journal()
        trade = _make_trade(size="25.00", price="0.40")
        j.log_trade(trade)
        j.update_trade_status("t001", "filled")
        j.update_trade_resolution("t001", "lost", Decimal("-10.00"))

        summary = j.get_portfolio_summary(Decimal("500"))
        j.close()

        # Resolved: exposure=0, cash = 500 - 0 + (-10) = 490
        assert summary["exposure"] == Decimal("0")
        assert summary["actual_pnl"] == Decimal("-10")
        assert summary["cash"] == Decimal("490")

    def test_portfolio_mixed(self) -> None:
        """Portfolio with both filled and resolved trades."""
        j = _make_journal()
        # Filled trade — still open
        t1 = _make_trade(trade_id="pm01", size="25.00")
        j.log_trade(t1)
        j.update_trade_status("pm01", "filled")

        # Resolved winner
        t2 = _make_trade(trade_id="pm02", market_id="mkt002", size="25.00", price="0.40")
        j.log_trade(t2)
        j.update_trade_status("pm02", "filled")
        j.update_trade_resolution("pm02", "won", Decimal("15.00"))

        summary = j.get_portfolio_summary(Decimal("500"))
        j.close()

        # exposure = 25 (pm01 still filled)
        # pnl = 15 (pm02 resolved)
        # cash = 500 - 25 + 15 = 490
        # total_value = 490 + 25 = 515
        assert summary["exposure"] == Decimal("25")
        assert summary["actual_pnl"] == Decimal("15")
        assert summary["cash"] == Decimal("490")
        assert summary["total_value"] == Decimal("515")


class TestPotentialPayoutNOTrade:
    """Tests for potential_payout calculation on NO-side trades."""

    def test_yes_trade_payout(self) -> None:
        """YES trade at 0.40: potential payout = (1 - 0.40) * 25 = $15."""
        j = _make_journal()
        trade = _make_trade(side="YES", price="0.40", size="25.00")
        j.log_trade(trade)
        detail = j.get_trade_detail("t001")
        j.close()

        assert detail is not None
        assert detail["potential_payout"] == Decimal("15.00")

    def test_no_trade_payout(self) -> None:
        """NO trade at 0.40 (YES price): effective entry = 0.60, payout = (1 - 0.60) * 25 = $10."""
        j = _make_journal()
        trade = _make_trade(side="NO", price="0.40", size="25.00")
        j.log_trade(trade)
        detail = j.get_trade_detail("t001")
        j.close()

        assert detail is not None
        # NO entry price = 1 - 0.40 = 0.60; payout = (1 - 0.60) * 25 = 10
        assert detail["potential_payout"] == Decimal("10.00")

    def test_no_trade_high_yes_price(self) -> None:
        """NO at 0.90 YES price: entry=0.10, payout=(1-0.10)*25=$22.50."""
        j = _make_journal()
        trade = _make_trade(trade_id="t002", side="NO", price="0.90", size="25.00")
        j.log_trade(trade)
        detail = j.get_trade_detail("t002")
        j.close()

        assert detail is not None
        assert detail["potential_payout"] == Decimal("22.50")


class TestLifecycleCountsEmptyEventDate:
    """Tests for lifecycle counts with empty event_date_ctx."""

    def test_empty_event_date_counted_as_open(self) -> None:
        """Trades with no event_date are treated as open, not ready."""
        j = _make_journal()
        # Trade with no context (empty event_date_ctx)
        trade = _make_trade(trade_id="nodate01")
        j.log_trade(trade)  # No market_context → empty event_date_ctx
        j.update_trade_status("nodate01", "filled")

        counts = j.get_lifecycle_counts()
        j.close()

        assert counts["open"] == 1
        assert counts["ready"] == 0
        assert counts["total"] == 1

    def test_mixed_with_empty_event_dates(self) -> None:
        """Mix of dated and undated trades counts correctly."""
        j = _make_journal()
        past = (date.today() - timedelta(days=2)).isoformat()
        future = (date.today() + timedelta(days=3)).isoformat()

        # Undated trade → open
        t1 = _make_trade(trade_id="mx01")
        j.log_trade(t1)
        j.update_trade_status("mx01", "filled")

        # Future dated → open
        t2 = _make_trade(trade_id="mx02", market_id="mkt002")
        j.log_trade(t2, market_context={"event_date": future, "location": "X"})
        j.update_trade_status("mx02", "filled")

        # Past dated → ready
        t3 = _make_trade(trade_id="mx03", market_id="mkt003")
        j.log_trade(t3, market_context={"event_date": past, "location": "Y"})
        j.update_trade_status("mx03", "filled")

        counts = j.get_lifecycle_counts()
        j.close()

        assert counts["open"] == 2  # undated + future
        assert counts["ready"] == 1  # past only
        assert counts["total"] == 3


class TestActualValueStorage:
    """Tests for actual_value and actual_value_unit on resolution."""

    def test_resolution_stores_actual_value(self) -> None:
        """Resolving a trade stores the actual weather value and unit."""
        j = _make_journal()
        trade = _make_trade(trade_id="av01")
        j.log_trade(trade)
        j.update_trade_status("av01", "filled")
        j.update_trade_resolution(
            "av01", "won", Decimal("15.00"),
            actual_value=68.0, actual_value_unit="\u00b0F",
        )

        detail = j.get_trade_detail("av01")
        j.close()

        assert detail is not None
        assert detail["actual_value"] == 68.0
        assert detail["actual_value_unit"] == "\u00b0F"

    def test_resolution_without_actual_value(self) -> None:
        """Resolving without actual_value stores None."""
        j = _make_journal()
        trade = _make_trade(trade_id="av02")
        j.log_trade(trade)
        j.update_trade_status("av02", "filled")
        j.update_trade_resolution("av02", "lost", Decimal("-10.00"))

        detail = j.get_trade_detail("av02")
        j.close()

        assert detail is not None
        assert detail["actual_value"] is None
        assert detail["actual_value_unit"] == ""


class TestForecastStorage:
    """Tests for NOAA forecast columns stored at bet time."""

    def test_forecast_data_stored_with_trade(self) -> None:
        """NOAA forecast data is stored when provided in context."""
        j = _make_journal()
        trade = _make_trade(trade_id="fc01")
        context = {
            "question": "Will Portland high temp be above 55F?",
            "location": "Portland, OR",
            "event_date": "2026-02-25",
            "metric": "temperature_high",
            "threshold": 55.0,
            "comparison": "above",
            "noaa_forecast_high": 62.0,
            "noaa_forecast_low": 44.0,
            "noaa_forecast_narrative": "Partly cloudy with highs in the low 60s.",
        }
        j.log_trade(trade, market_context=context)

        detail = j.get_trade_detail("fc01")
        j.close()

        assert detail is not None
        assert detail["noaa_forecast_high"] == 62.0
        assert detail["noaa_forecast_low"] == 44.0
        assert detail["noaa_forecast_narrative"] == "Partly cloudy with highs in the low 60s."

    def test_forecast_data_empty_when_not_provided(self) -> None:
        """Forecast fields are None/empty when not in context."""
        j = _make_journal()
        trade = _make_trade(trade_id="fc02")
        j.log_trade(trade)

        detail = j.get_trade_detail("fc02")
        j.close()

        assert detail is not None
        assert detail["noaa_forecast_high"] is None
        assert detail["noaa_forecast_low"] is None
        assert detail["noaa_forecast_narrative"] == ""


class TestGetReportData:
    """Tests for get_report_data summary statistics."""

    def test_empty_report(self) -> None:
        """Empty journal returns zero stats."""
        j = _make_journal()
        report = j.get_report_data(30)
        j.close()

        assert report["total_trades"] == 0
        assert report["filled_trades"] == 0
        assert report["resolved_trades"] == 0
        assert report["simulated_pnl"] == Decimal("0")
        assert report["actual_pnl"] == Decimal("0")

    def test_report_with_filled_trades(self) -> None:
        """Report counts filled trades and computes simulated P&L."""
        j = _make_journal()
        t1 = _make_trade(trade_id="rp01", edge="0.20", size="25.00")
        t2 = _make_trade(trade_id="rp02", market_id="mkt002", edge="0.15", size="20.00")
        j.log_trade(t1)
        j.log_trade(t2)
        j.update_trade_status("rp01", "filled")
        j.update_trade_status("rp02", "filled")

        report = j.get_report_data(30)
        j.close()

        assert report["filled_trades"] == 2
        assert report["wins"] == 2  # Both have positive edge
        expected_pnl = Decimal("0.20") * Decimal("25") + Decimal("0.15") * Decimal("20")
        assert report["simulated_pnl"] == expected_pnl

    def test_report_with_resolved_trades(self) -> None:
        """Report counts resolved trades with actual P&L."""
        j = _make_journal()
        t1 = _make_trade(trade_id="rp03")
        j.log_trade(t1)
        j.update_trade_status("rp03", "filled")
        j.update_trade_resolution("rp03", "won", Decimal("15.00"))

        report = j.get_report_data(30)
        j.close()

        assert report["resolved_trades"] == 1
        assert report["actual_wins"] == 1
        assert report["actual_pnl"] == Decimal("15.00")


class TestDailySnapshots:
    """Tests for save_daily_snapshot and get_snapshots."""

    def test_save_and_retrieve_snapshot(self) -> None:
        """Snapshot can be saved and retrieved."""
        j = _make_journal()
        today = date.today()
        j.save_daily_snapshot(
            snapshot_date=today,
            cash=Decimal("475"),
            total_value=Decimal("500"),
            daily_pnl=Decimal("5.00"),
            open_positions=2,
            trades_today=3,
        )

        snapshots = j.get_snapshots(days=7)
        j.close()

        assert len(snapshots) == 1
        assert snapshots[0]["snapshot_date"] == today.isoformat()
        assert snapshots[0]["trades_today"] == 3

    def test_snapshot_upserts(self) -> None:
        """Saving snapshot for same date updates rather than duplicates."""
        j = _make_journal()
        today = date.today()
        j.save_daily_snapshot(
            snapshot_date=today,
            cash=Decimal("475"),
            total_value=Decimal("500"),
            daily_pnl=Decimal("5.00"),
            open_positions=2,
            trades_today=1,
        )
        j.save_daily_snapshot(
            snapshot_date=today,
            cash=Decimal("460"),
            total_value=Decimal("500"),
            daily_pnl=Decimal("10.00"),
            open_positions=3,
            trades_today=2,
        )

        snapshots = j.get_snapshots(days=7)
        j.close()

        assert len(snapshots) == 1
        assert snapshots[0]["trades_today"] == 2  # Updated value
