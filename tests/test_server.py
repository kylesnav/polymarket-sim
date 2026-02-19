"""Tests for the FastAPI server endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.server import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tc() -> TestClient:
    return TestClient(app)


def _mock_journal() -> MagicMock:
    j = MagicMock()
    j.get_lifecycle_counts.return_value = {
        "open": 2, "ready": 1, "resolved": 5, "total": 8,
    }
    j.close.return_value = None
    return j


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

class TestStaticEndpoints:
    """Tests for static file serving."""

    def test_index_returns_html(self, tc: TestClient) -> None:
        resp = tc.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Status & Config
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    """Tests for GET /api/status."""

    @patch("src.server.Journal")
    @patch("src.server._load_settings")
    def test_returns_status(
        self, mock_settings: MagicMock, mock_journal_cls: MagicMock, tc: TestClient,
    ) -> None:
        settings = MagicMock()
        settings.max_bankroll = 500
        settings.position_cap_pct = 0.05
        settings.kelly_fraction = 0.25
        settings.min_edge_threshold = 0.10
        settings.daily_loss_limit_pct = 0.05
        settings.kill_switch = False
        settings.log_level = "INFO"
        mock_settings.return_value = settings

        mock_journal_cls.return_value = _mock_journal()

        resp = tc.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["max_bankroll"] == 500
        assert "open_bets" in data
        assert "resolved_count" in data


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

class TestTradesEndpoints:
    """Tests for trade-related endpoints."""

    @patch("src.server.Journal")
    def test_get_trades_returns_list(self, mock_journal_cls: MagicMock, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trades_with_context.return_value = [
            {"trade_id": "t1", "market_id": "m1", "status": "filled"},
        ]
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["trades"][0]["trade_id"] == "t1"

    @patch("src.server.Journal")
    def test_get_trades_with_status_filter(
        self, mock_journal_cls: MagicMock, tc: TestClient,
    ) -> None:
        journal = _mock_journal()
        journal.get_trades_with_context.return_value = []
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/trades?status=resolved&outcome=won")
        assert resp.status_code == 200
        journal.get_trades_with_context.assert_called_once_with(90, "resolved", "won")

    @patch("src.server.Journal")
    def test_get_trade_detail_found(self, mock_journal_cls: MagicMock, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trade_detail.return_value = {
            "trade_id": "abc123",
            "market_id": "m1",
            "status": "filled",
        }
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/trades/abc123")
        assert resp.status_code == 200
        assert resp.json()["trade_id"] == "abc123"

    @patch("src.server.Journal")
    def test_get_trade_detail_not_found(self, mock_journal_cls: MagicMock, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trade_detail.return_value = None
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/trades/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

class TestPortfolioEndpoint:
    """Tests for GET /api/portfolio."""

    @patch("src.server.Journal")
    @patch("src.server._load_settings")
    def test_returns_portfolio_summary(
        self, mock_settings: MagicMock, mock_journal_cls: MagicMock, tc: TestClient,
    ) -> None:
        settings = MagicMock()
        settings.max_bankroll = 500
        mock_settings.return_value = settings

        journal = _mock_journal()
        journal.get_portfolio_summary.return_value = {
            "cash": Decimal("450"),
            "exposure": Decimal("50"),
            "total_value": Decimal("500"),
        }
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cash"] == 450.0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReportEndpoint:
    """Tests for GET /api/report."""

    @patch("src.server.Journal")
    def test_returns_report_data(self, mock_journal_cls: MagicMock, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_report_data.return_value = {
            "days": 30,
            "total_trades": 10,
            "wins": 6,
            "losses": 4,
        }
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/report?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 10


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

class TestSnapshotsEndpoint:
    """Tests for GET /api/snapshots."""

    @patch("src.server.Journal")
    def test_returns_snapshots(self, mock_journal_cls: MagicMock, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_snapshots.return_value = [
            {"snapshot_date": "2027-03-01", "total_value": "500"},
        ]
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/snapshots?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["snapshots"]) == 1


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

class TestLogsEndpoint:
    """Tests for GET /api/logs."""

    def test_returns_logs(self, tc: TestClient) -> None:
        resp = tc.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
        assert "cursor" in data

    def test_since_parameter_filters(self, tc: TestClient) -> None:
        resp = tc.get("/api/logs?since=999999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["logs"] == []


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

class TestScanEndpoint:
    """Tests for POST /api/scan."""

    @patch("src.server._make_simulator")
    @patch("src.server._load_settings")
    def test_scan_returns_signals(
        self, mock_settings: MagicMock, mock_sim: MagicMock, tc: TestClient,
    ) -> None:
        settings = MagicMock()
        mock_settings.return_value = settings

        sim = MagicMock()
        sim.run_scan.return_value = []
        sim.last_markets = []
        mock_sim.return_value = sim

        resp = tc.post("/api/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    @patch("src.server._make_simulator")
    @patch("src.server._load_settings")
    def test_scan_error_returns_500(
        self, mock_settings: MagicMock, mock_sim: MagicMock, tc: TestClient,
    ) -> None:
        mock_settings.return_value = MagicMock()
        sim = MagicMock()
        sim.run_scan.side_effect = RuntimeError("API failed")
        mock_sim.return_value = sim

        resp = tc.post("/api/scan")
        assert resp.status_code == 500
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Sim
# ---------------------------------------------------------------------------

class TestSimEndpoint:
    """Tests for POST /api/sim."""

    @patch("src.server._make_simulator")
    @patch("src.server._load_settings")
    def test_sim_no_signals(
        self, mock_settings: MagicMock, mock_sim: MagicMock, tc: TestClient,
    ) -> None:
        mock_settings.return_value = MagicMock()
        sim = MagicMock()
        sim.run_scan.return_value = []
        mock_sim.return_value = sim

        resp = tc.post("/api/sim")
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "No actionable signals found."


# ---------------------------------------------------------------------------
# Sim Execute (selective)
# ---------------------------------------------------------------------------

class TestSimExecuteEndpoint:
    """Tests for POST /api/sim/execute."""

    @patch("src.server._make_simulator")
    @patch("src.server._load_settings")
    def test_execute_no_market_ids_returns_400(
        self, mock_settings: MagicMock, mock_sim: MagicMock, tc: TestClient,
    ) -> None:
        mock_settings.return_value = MagicMock()
        resp = tc.post("/api/sim/execute", json={"market_ids": []})
        assert resp.status_code == 400

    @patch("src.server._make_simulator")
    @patch("src.server._load_settings")
    def test_execute_no_matching_signals(
        self, mock_settings: MagicMock, mock_sim: MagicMock, tc: TestClient,
    ) -> None:
        mock_settings.return_value = MagicMock()
        sim = MagicMock()
        sim.run_scan.return_value = []
        mock_sim.return_value = sim

        resp = tc.post("/api/sim/execute", json={"market_ids": ["mkt-1"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["trades"] == []


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

class TestResolveEndpoint:
    """Tests for POST /api/resolve."""

    @patch("src.server.NOAAClient")
    @patch("src.server.Journal")
    def test_resolve_returns_stats(
        self, mock_journal_cls: MagicMock, mock_noaa_cls: MagicMock, tc: TestClient,
    ) -> None:
        journal = _mock_journal()
        noaa = MagicMock()
        mock_journal_cls.return_value = journal
        mock_noaa_cls.return_value = noaa

        with patch("src.server.resolve_trades") as mock_resolve:
            mock_resolve.return_value = {
                "resolved_count": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": Decimal("0"),
            }
            resp = tc.post("/api/resolve")

        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_count"] == 0


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

class TestBacktestEndpoint:
    """Tests for POST /api/backtest."""

    @patch("src.server.Backtester")
    @patch("src.server._load_settings")
    def test_backtest_returns_results(
        self, mock_settings: MagicMock, mock_bt_cls: MagicMock, tc: TestClient,
    ) -> None:
        settings = MagicMock()
        settings.max_bankroll = 500
        settings.min_edge_threshold = 0.10
        settings.kelly_fraction = 0.25
        settings.position_cap_pct = 0.05
        mock_settings.return_value = settings

        bt = MagicMock()
        result = MagicMock()
        result.trades = []
        result.wins = 0
        result.losses = 0
        result.total_pnl = Decimal("0")
        result.markets_scanned = 5
        result.markets_skipped = 2
        result.caveat = "Test caveat"
        bt.run.return_value = result
        mock_bt_cls.return_value = bt

        resp = tc.post("/api/backtest", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["markets_scanned"] == 5


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestSettingsEndpoint:
    """Tests for PUT /api/settings."""

    @patch("src.server.Journal")
    @patch("src.server._load_settings")
    def test_update_settings(
        self, mock_settings: MagicMock, mock_journal_cls: MagicMock, tc: TestClient, tmp_path: Any,
    ) -> None:
        # This test exercises the endpoint but relies on writing .env
        # We patch to avoid side effects
        settings = MagicMock()
        settings.max_bankroll = 500
        settings.position_cap_pct = 0.05
        settings.kelly_fraction = 0.25
        settings.min_edge_threshold = 0.10
        settings.daily_loss_limit_pct = 0.05
        settings.kill_switch = False
        settings.log_level = "INFO"
        mock_settings.return_value = settings
        mock_journal_cls.return_value = _mock_journal()

        with patch("src.server.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = "MAX_BANKROLL=500\n"
            mock_path_cls.return_value = mock_path

            resp = tc.put("/api/settings", json={"max_bankroll": 1000})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Kill Switch
# ---------------------------------------------------------------------------

class TestKillSwitchEndpoint:
    """Tests for PUT /api/kill-switch."""

    @patch("src.server.Journal")
    @patch("src.server._load_settings")
    def test_toggle_kill_switch(
        self, mock_settings: MagicMock, mock_journal_cls: MagicMock, tc: TestClient,
    ) -> None:
        settings = MagicMock()
        settings.max_bankroll = 500
        settings.position_cap_pct = 0.05
        settings.kelly_fraction = 0.25
        settings.min_edge_threshold = 0.10
        settings.daily_loss_limit_pct = 0.05
        settings.kill_switch = True
        settings.log_level = "INFO"
        mock_settings.return_value = settings
        mock_journal_cls.return_value = _mock_journal()

        with patch("src.server.Path") as mock_path_cls:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = "KILL_SWITCH=false\n"
            mock_path_cls.return_value = mock_path

            resp = tc.put("/api/kill-switch", json={"enabled": True})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# JSON encoder
# ---------------------------------------------------------------------------

class TestJsonEncoder:
    """Tests for the custom JSON encoder used in responses."""

    @patch("src.server.Journal")
    def test_handles_decimal_in_response(
        self, mock_journal_cls: MagicMock, tc: TestClient,
    ) -> None:
        journal = _mock_journal()
        journal.get_report_data.return_value = {
            "days": 30,
            "total_trades": 0,
            "simulated_pnl": Decimal("12.50"),
            "actual_pnl": Decimal("0"),
        }
        mock_journal_cls.return_value = journal

        resp = tc.get("/api/report")
        assert resp.status_code == 200
        data = resp.json()
        assert data["simulated_pnl"] == 12.5
