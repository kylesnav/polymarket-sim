"""FastAPI server for the Weather Edge Tracker."""

from __future__ import annotations

import collections
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from src.backtest import Backtester
from src.config import Settings
from src.journal import Journal
from src.noaa import NOAAClient
from src.resolver import resolve_trades
from src.simulator import Simulator

# ── Log buffer for /api/logs endpoint ────────────────

_log_buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=500)
_log_lock = Lock()
_log_counter = 0


def _buffer_log_processor(
    _logger: Any,  # noqa: ANN401
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that copies log entries to the ring buffer."""
    global _log_counter  # noqa: PLW0603
    entry = {
        "timestamp": event_dict.get("timestamp", ""),
        "level": event_dict.get("level", method_name),
        "event": event_dict.get("event", ""),
    }
    # Include extra fields (skip internal keys)
    skip = {"timestamp", "level", "event", "_record", "_from_structlog"}
    for k, v in event_dict.items():
        if k not in skip:
            entry[k] = str(v)
    with _log_lock:
        _log_counter += 1
        entry["id"] = _log_counter
        _log_buffer.append(entry)
    return event_dict


# Configure structlog so bot modules can log
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _buffer_log_processor,
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run trade context backfill once at server startup."""
    journal = Journal()
    try:
        journal.backfill_trade_context()
    finally:
        journal.close()
    yield


app = FastAPI(title="Weather Edge Tracker", lifespan=_lifespan)


class _Encoder(json.JSONEncoder):
    """JSON encoder that handles Decimal, date, and datetime."""

    def default(self, o: object) -> Any:  # noqa: ANN401
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        return super().default(o)  # type: ignore[arg-type]


def _json(data: Any) -> JSONResponse:  # noqa: ANN401
    """Serialize data to JSONResponse with Decimal/date support."""
    content: Any = json.loads(json.dumps(data, cls=_Encoder))
    return JSONResponse(content=content)


def _load_settings() -> Settings:
    """Load settings from .env, creating file with defaults if needed."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            env_path.write_text(example.read_text())
    return Settings()


def _make_simulator(settings: Settings) -> Simulator:
    """Create a Simulator from current settings."""
    return Simulator(
        bankroll=Decimal(str(settings.max_bankroll)),
        min_edge=Decimal(str(settings.min_edge_threshold)),
        kelly_fraction=Decimal(str(settings.kelly_fraction)),
        position_cap_pct=Decimal(str(settings.position_cap_pct)),
        max_bankroll=Decimal(str(settings.max_bankroll)),
        daily_loss_limit_pct=Decimal(str(settings.daily_loss_limit_pct)),
        kill_switch=settings.kill_switch,
    )


def _enrich_signals(
    signals: list[Any],  # noqa: ANN401
    sim: Simulator,
) -> list[dict[str, Any]]:
    """Add market question/location/event info to signal dicts."""
    market_lookup = {m.market_id: m for m in sim.last_markets}
    enriched: list[dict[str, Any]] = []
    for s in signals:
        d: dict[str, Any] = s.model_dump()
        market = market_lookup.get(s.market_id)
        if market:
            d["question"] = market.question
            d["location"] = market.location
            d["event_date"] = market.event_date.isoformat()
            d["metric"] = market.metric
            d["threshold"] = market.threshold
            # Compute potential payout for display
            price = float(d.get("market_price", 0))
            size = float(d.get("recommended_size", 0))
            d["potential_payout"] = round((1.0 - price) * size, 2)
        enriched.append(d)
    return enriched


# ── Static ──────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    """Serve the admin panel HTML."""
    return FileResponse(Path(__file__).parent.parent / "admin-panel.html")


# ── Status & Config ─────────────────────────────────

@app.get("/api/status")
def get_status() -> JSONResponse:
    """Return current config + lifecycle counts."""
    settings = _load_settings()
    journal = Journal()
    try:
        lifecycle = journal.get_lifecycle_counts()
        return _json({
            "max_bankroll": settings.max_bankroll,
            "position_cap_pct": settings.position_cap_pct,
            "kelly_fraction": settings.kelly_fraction,
            "min_edge_threshold": settings.min_edge_threshold,
            "daily_loss_limit_pct": settings.daily_loss_limit_pct,
            "kill_switch": settings.kill_switch,
            "log_level": settings.log_level,
            "unresolved_trades": lifecycle["open"] + lifecycle["ready"],
            "open_bets": lifecycle["open"],
            "ready_to_resolve": lifecycle["ready"],
            "resolved_count": lifecycle["resolved"],
            "total_trades": lifecycle["total"],
        })
    finally:
        journal.close()


@app.put("/api/settings")
async def update_settings(request: Request) -> JSONResponse:
    """Update .env config values and return new settings."""
    body = await request.json()
    env_path = Path(".env")

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    key_map = {
        "max_bankroll": "MAX_BANKROLL",
        "position_cap_pct": "POSITION_CAP_PCT",
        "kelly_fraction": "KELLY_FRACTION",
        "min_edge_threshold": "MIN_EDGE_THRESHOLD",
        "daily_loss_limit_pct": "DAILY_LOSS_LIMIT_PCT",
        "kill_switch": "KILL_SWITCH",
        "log_level": "LOG_LEVEL",
    }

    for py_key, env_key in key_map.items():
        if py_key in body:
            value = body[py_key]
            if isinstance(value, bool):
                value = "true" if value else "false"
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{env_key}="):
                    lines[i] = f"{env_key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{env_key}={value}")

    env_path.write_text("\n".join(lines) + "\n")
    return get_status()


@app.put("/api/kill-switch")
async def toggle_kill_switch(request: Request) -> JSONResponse:
    """Toggle kill switch on/off."""
    body = await request.json()
    enabled = body.get("enabled", False)
    env_path = Path(".env")
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    value = "true" if enabled else "false"
    found = False
    for i, line in enumerate(lines):
        if line.startswith("KILL_SWITCH="):
            lines[i] = f"KILL_SWITCH={value}"
            found = True
            break
    if not found:
        lines.append(f"KILL_SWITCH={value}")

    env_path.write_text("\n".join(lines) + "\n")
    return get_status()


# ── Actions (CLI parity) ───────────────────────────

@app.post("/api/scan")
def run_scan() -> JSONResponse:
    """Scan for weather markets with edge. Equivalent to `cli scan`."""
    settings = _load_settings()
    sim = _make_simulator(settings)
    try:
        signals = sim.run_scan()
        return _json({
            "signals": _enrich_signals(signals, sim),
            "count": len(signals),
        })
    except Exception as e:
        logger.error("scan_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        sim.close()


@app.post("/api/sim")
def run_sim() -> JSONResponse:
    """Run full simulation: scan + execute paper trades. Equivalent to `cli sim`."""
    settings = _load_settings()
    sim = _make_simulator(settings)
    try:
        signals = sim.run_scan()
        if not signals:
            return _json({
                "signals": [],
                "trades": [],
                "portfolio": None,
                "message": "No actionable signals found.",
            })

        trades = sim.execute_signals(signals)
        portfolio = sim.get_portfolio()
        return _json({
            "signals": _enrich_signals(signals, sim),
            "trades": [t.model_dump() for t in trades],
            "portfolio": portfolio.model_dump(),
        })
    except Exception as e:
        logger.error("sim_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        sim.close()


@app.post("/api/sim/execute")
async def run_sim_execute(request: Request) -> JSONResponse:
    """Execute paper trades for selected market IDs only (bet slip confirm).

    Accepts JSON body with {"market_ids": ["id1", "id2", ...]}.
    Scans markets, filters signals to only the selected IDs, and executes.
    """
    body = await request.json()
    market_ids: list[str] = body.get("market_ids", [])
    if not market_ids:
        return JSONResponse(status_code=400, content={"error": "No market_ids provided"})

    settings = _load_settings()
    sim = _make_simulator(settings)
    try:
        signals = sim.run_scan()
        selected = [s for s in signals if s.market_id in market_ids]
        if not selected:
            return _json({
                "trades": [],
                "skipped": len(market_ids),
                "message": "None of the selected markets had actionable signals.",
            })
        trades = sim.execute_signals(selected)
        return _json({
            "trades": [t.model_dump() for t in trades],
            "signals": _enrich_signals(selected, sim),
            "skipped": len(market_ids) - len(trades),
            "message": f"Placed {len(trades)} bet(s).",
        })
    except Exception as e:
        logger.error("sim_execute_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        sim.close()


@app.post("/api/resolve")
def run_resolve() -> JSONResponse:
    """Resolve unresolved trades against actual weather. Equivalent to `cli resolve`."""
    journal = Journal()
    noaa = NOAAClient()
    try:
        stats = resolve_trades(journal, noaa)
        return _json(stats)
    except Exception as e:
        logger.error("resolve_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        noaa.close()
        journal.close()


@app.post("/api/backtest")
async def run_backtest(request: Request) -> JSONResponse:
    """Run backtest against recently resolved weather markets.

    Accepts optional JSON body with lookback_days, price_offset_days, bankroll.
    """
    body: dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()

    settings = _load_settings()
    lookback = int(body.get("lookback_days", 7))
    price_offset = int(body.get("price_offset_days", 2))
    bankroll = float(body.get("bankroll", settings.max_bankroll))

    backtester = Backtester(
        bankroll=Decimal(str(bankroll)),
        min_edge=Decimal(str(settings.min_edge_threshold)),
        kelly_fraction=Decimal(str(settings.kelly_fraction)),
        position_cap_pct=Decimal(str(settings.position_cap_pct)),
        lookback_days=lookback,
        price_offset_days=price_offset,
    )

    try:
        result = backtester.run()
        trade_count = len(result.trades)
        return _json({
            "trades": [t.model_dump() for t in result.trades],
            "wins": result.wins,
            "losses": result.losses,
            "total_pnl": result.total_pnl,
            "markets_scanned": result.markets_scanned,
            "markets_skipped": result.markets_skipped,
            "caveat": result.caveat,
            "win_rate": result.wins / trade_count if trade_count else 0,
        })
    except Exception as e:
        logger.error("backtest_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        backtester.close()


# ── Data Queries ────────────────────────────────────

@app.get("/api/report")
def get_report(days: int = 30) -> JSONResponse:
    """Get report data. Equivalent to `cli report --days N`."""
    journal = Journal()
    try:
        data = journal.get_report_data(days)
        return _json(data)
    finally:
        journal.close()


@app.get("/api/portfolio")
def get_portfolio() -> JSONResponse:
    """Get computed portfolio state from trade history."""
    settings = _load_settings()
    journal = Journal()
    try:
        summary = journal.get_portfolio_summary(Decimal(str(settings.max_bankroll)))
        return _json(summary)
    finally:
        journal.close()


@app.get("/api/trades/{trade_id}")
def get_trade_detail(trade_id: str) -> JSONResponse:
    """Get a single trade with full market context."""
    journal = Journal()
    try:
        detail = journal.get_trade_detail(trade_id)
        if detail is None:
            return JSONResponse(status_code=404, content={"error": "Trade not found"})
        return _json(detail)
    finally:
        journal.close()


@app.get("/api/trades")
def get_trades(
    days: int = 90,
    status: str | None = None,
    outcome: str | None = None,
) -> JSONResponse:
    """Get trade history with market context and lifecycle state."""
    journal = Journal()
    try:
        trades = journal.get_trades_with_context(days, status, outcome)
        return _json({
            "trades": trades,
            "count": len(trades),
        })
    finally:
        journal.close()


@app.get("/api/snapshots")
def get_snapshots(days: int = 60) -> JSONResponse:
    """Get daily portfolio snapshots for charting."""
    journal = Journal()
    try:
        snapshots = journal.get_snapshots(days)
        return _json({"snapshots": snapshots})
    finally:
        journal.close()


@app.get("/api/logs")
def get_logs(since: int = 0) -> JSONResponse:
    """Get recent log entries for the activity log viewer.

    Args:
        since: Return only entries with id > this value (cursor-based polling).
    """
    with _log_lock:
        entries = [e for e in _log_buffer if e.get("id", 0) > since]
    return _json({
        "logs": entries,
        "cursor": _log_counter,
    })
