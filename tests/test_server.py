"""Tests for the FastAPI server endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.server import app, get_journal, get_settings, get_simulator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.max_bankroll = 500
    s.position_cap_pct = 0.05
    s.kelly_fraction = 0.25
    s.min_edge_threshold = 0.10
    s.daily_loss_limit_pct = 0.05
    s.kill_switch = False
    s.log_level = "INFO"
    s.min_volume = 1000.0
    s.max_spread = 0.05
    s.max_forecast_horizon_days = 5
    s.max_forecast_age_hours = 12.0
    return s


def _mock_journal() -> MagicMock:
    j = MagicMock()
    j.get_lifecycle_counts.return_value = {
        "open": 2, "ready": 1, "resolved": 5, "total": 8,
    }
    j.close.return_value = None
    return j


def _mock_simulator() -> MagicMock:
    sim = MagicMock()
    sim.run_scan.return_value = []
    sim.last_markets = []
    sim.close.return_value = None
    return sim


@pytest.fixture
def tc() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_overrides() -> Any:  # noqa: ANN401
    """Clear dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


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

    def test_returns_status(self, tc: TestClient) -> None:
        settings = _mock_settings()
        journal = _mock_journal()

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_journal] = lambda: journal

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

    def test_get_trades_returns_list(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trades_with_context.return_value = [
            {"trade_id": "t1", "market_id": "m1", "status": "filled"},
        ]
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["trades"][0]["trade_id"] == "t1"

    def test_get_trades_with_status_filter(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trades_with_context.return_value = []
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/trades?status=resolved&outcome=won")
        assert resp.status_code == 200
        journal.get_trades_with_context.assert_called_once_with(90, "resolved", "won")

    def test_get_trade_detail_found(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trade_detail.return_value = {
            "trade_id": "abc123",
            "market_id": "m1",
            "status": "filled",
        }
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/trades/abc123")
        assert resp.status_code == 200
        assert resp.json()["trade_id"] == "abc123"

    def test_get_trade_detail_not_found(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_trade_detail.return_value = None
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/trades/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

class TestPortfolioEndpoint:
    """Tests for GET /api/portfolio."""

    def test_returns_portfolio_summary(self, tc: TestClient) -> None:
        settings = _mock_settings()
        journal = _mock_journal()
        journal.get_portfolio_summary.return_value = {
            "cash": Decimal("450"),
            "exposure": Decimal("50"),
            "total_value": Decimal("500"),
        }
        journal.get_open_positions_with_pnl.return_value = {
            "positions": [],
            "summary": {
                "position_count": 0,
                "total_exposure": Decimal("0"),
                "total_max_profit": Decimal("0"),
                "total_max_loss": Decimal("0"),
                "total_expected_pnl": Decimal("0"),
                "total_expected_return": Decimal("0"),
            },
        }

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cash"] == 450.0
        assert data["estimated_expected_pnl"] == 0.0


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class TestPositionsEndpoint:
    """Tests for GET /api/positions."""

    def test_returns_positions_and_summary(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_open_positions_with_pnl.return_value = {
            "positions": [
                {
                    "trade_id": "abc123",
                    "market_id": "mkt1",
                    "question": "Will NYC temp > 40?",
                    "side": "YES",
                    "size": Decimal("50"),
                    "entry_price": Decimal("0.40"),
                    "noaa_probability": Decimal("0.70"),
                    "edge": Decimal("0.30"),
                    "max_profit": Decimal("75.00"),
                    "max_loss": Decimal("-50.00"),
                    "expected_pnl": Decimal("37.50"),
                    "expected_return": Decimal("0.7500"),
                    "event_date": "2026-02-25",
                    "days_until_event": 3,
                },
            ],
            "summary": {
                "position_count": 1,
                "total_exposure": Decimal("50.00"),
                "total_max_profit": Decimal("75.00"),
                "total_max_loss": Decimal("-50.00"),
                "total_expected_pnl": Decimal("37.50"),
                "total_expected_return": Decimal("0.7500"),
            },
        }
        app.dependency_overrides[get_journal] = lambda: journal
        resp = tc.get("/api/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["position_count"] == 1
        assert data["positions"][0]["side"] == "YES"
        assert data["positions"][0]["max_profit"] == 75.0
        assert data["summary"]["total_expected_pnl"] == 37.5

    def test_returns_empty_positions(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_open_positions_with_pnl.return_value = {
            "positions": [],
            "summary": {
                "position_count": 0,
                "total_exposure": Decimal("0"),
                "total_max_profit": Decimal("0"),
                "total_max_loss": Decimal("0"),
                "total_expected_pnl": Decimal("0"),
                "total_expected_return": Decimal("0"),
            },
        }
        app.dependency_overrides[get_journal] = lambda: journal
        resp = tc.get("/api/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"]["position_count"] == 0
        assert data["positions"] == []

    def test_handles_error(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_open_positions_with_pnl.side_effect = RuntimeError("db error")
        app.dependency_overrides[get_journal] = lambda: journal
        resp = tc.get("/api/positions")
        assert resp.status_code == 500
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReportEndpoint:
    """Tests for GET /api/report."""

    def test_returns_report_data(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_report_data.return_value = {
            "days": 30,
            "total_trades": 10,
            "wins": 6,
            "losses": 4,
        }
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/report?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_trades"] == 10


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

class TestSnapshotsEndpoint:
    """Tests for GET /api/snapshots."""

    def test_returns_snapshots(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_snapshots.return_value = [
            {"snapshot_date": "2027-03-01", "total_value": "500"},
        ]
        app.dependency_overrides[get_journal] = lambda: journal

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

    def test_scan_returns_signals(self, tc: TestClient) -> None:
        sim = _mock_simulator()
        app.dependency_overrides[get_simulator] = lambda: sim

        resp = tc.post("/api/scan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    def test_scan_error_returns_500(self, tc: TestClient) -> None:
        sim = _mock_simulator()
        sim.run_scan.side_effect = RuntimeError("API failed")
        app.dependency_overrides[get_simulator] = lambda: sim

        resp = tc.post("/api/scan")
        assert resp.status_code == 500
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# Sim
# ---------------------------------------------------------------------------

class TestSimEndpoint:
    """Tests for POST /api/sim."""

    def test_sim_no_signals(self, tc: TestClient) -> None:
        sim = _mock_simulator()
        app.dependency_overrides[get_simulator] = lambda: sim

        resp = tc.post("/api/sim")
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "No actionable signals found."


# ---------------------------------------------------------------------------
# Sim Execute (selective)
# ---------------------------------------------------------------------------

class TestSimExecuteEndpoint:
    """Tests for POST /api/sim/execute."""

    def test_execute_no_market_ids_returns_400(self, tc: TestClient) -> None:
        sim = _mock_simulator()
        app.dependency_overrides[get_simulator] = lambda: sim

        resp = tc.post("/api/sim/execute", json={"market_ids": []})
        assert resp.status_code == 400

    def test_execute_no_matching_signals(self, tc: TestClient) -> None:
        sim = _mock_simulator()
        app.dependency_overrides[get_simulator] = lambda: sim

        resp = tc.post("/api/sim/execute", json={"market_ids": ["mkt-1"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["trades"] == []


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

class TestResolveEndpoint:
    """Tests for POST /api/resolve."""

    def test_resolve_returns_stats(self, tc: TestClient) -> None:
        journal = _mock_journal()
        app.dependency_overrides[get_journal] = lambda: journal

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
# Settings
# ---------------------------------------------------------------------------

class TestSettingsEndpoint:
    """Tests for PUT /api/settings."""

    def test_update_settings(self, tc: TestClient) -> None:
        journal = _mock_journal()
        app.dependency_overrides[get_journal] = lambda: journal

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

    def test_toggle_kill_switch(self, tc: TestClient) -> None:
        journal = _mock_journal()
        app.dependency_overrides[get_journal] = lambda: journal

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

    def test_handles_decimal_in_response(self, tc: TestClient) -> None:
        journal = _mock_journal()
        journal.get_report_data.return_value = {
            "days": 30,
            "total_trades": 0,
            "simulated_pnl": Decimal("12.50"),
            "actual_pnl": Decimal("0"),
        }
        app.dependency_overrides[get_journal] = lambda: journal

        resp = tc.get("/api/report")
        assert resp.status_code == 200
        data = resp.json()
        assert data["simulated_pnl"] == 12.5
