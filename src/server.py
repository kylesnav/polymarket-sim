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
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

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
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
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


# ── Dependency Injection ─────────────────────────────

@lru_cache(maxsize=1)
def _cached_settings() -> Settings:
    """Load and cache settings from .env."""
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        if example.exists():
            env_path.write_text(example.read_text())
    return Settings()


def _invalidate_settings_cache() -> None:
    """Clear the settings cache so next call reloads from .env."""
    _cached_settings.cache_clear()


def get_settings() -> Settings:
    """FastAPI dependency: provides Settings instance."""
    return _cached_settings()


def get_journal() -> Journal:
    """FastAPI dependency: provides a Journal instance.

    Yields a Journal and closes it after the request.
    """
    return Journal()


def get_simulator(settings: Annotated[Settings, Depends(get_settings)]) -> Simulator:
    """FastAPI dependency: provides a Simulator built from Settings."""
    return Simulator(
        bankroll=Decimal(str(settings.max_bankroll)),
        min_edge=Decimal(str(settings.min_edge_threshold)),
        kelly_fraction=Decimal(str(settings.kelly_fraction)),
        position_cap_pct=Decimal(str(settings.position_cap_pct)),
        max_bankroll=Decimal(str(settings.max_bankroll)),
        daily_loss_limit_pct=Decimal(str(settings.daily_loss_limit_pct)),
        kill_switch=settings.kill_switch,
        min_volume=Decimal(str(settings.min_volume)),
        max_spread=Decimal(str(settings.max_spread)),
        max_forecast_horizon_days=settings.max_forecast_horizon_days,
        max_forecast_age_hours=settings.max_forecast_age_hours,
    )


# ── Lifespan ─────────────────────────────────────────

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


# ── Global Exception Handler ────────────────────────

@app.exception_handler(Exception)
async def _global_exception_handler(  # pyright: ignore[reportUnusedFunction]
    _request: Request, exc: Exception
) -> JSONResponse:
    """Catch unhandled exceptions and return a JSON error."""
    logger.error("unhandled_exception", error=str(exc), type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "type": type(exc).__name__},
    )


# ── JSON Encoding ───────────────────────────────────

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
            # Compute potential payout (size is dollars invested, not contracts)
            price = float(d.get("market_price", 0))
            size = float(d.get("recommended_size", 0))
            side = d.get("side", "YES")
            effective_price = price if side == "YES" else 1.0 - price
            if effective_price > 0:
                d["potential_payout"] = round(size * (1.0 - effective_price) / effective_price, 2)
            else:
                d["potential_payout"] = 0.0
        enriched.append(d)
    return enriched


# ── Static ──────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    """Serve the admin panel HTML."""
    return FileResponse(Path(__file__).parent.parent / "admin-panel.html")


# ── Status & Config ─────────────────────────────────

@app.get("/api/status")
def get_status(
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[Journal, Depends(get_journal)],
) -> JSONResponse:
    """Return current config + lifecycle counts."""
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
    _invalidate_settings_cache()

    # Re-fetch status with fresh settings
    settings = get_settings()
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
    _invalidate_settings_cache()

    settings = get_settings()
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


# ── Actions (CLI parity) ───────────────────────────

@app.post("/api/scan")
def run_scan(
    sim: Annotated[Simulator, Depends(get_simulator)],
) -> JSONResponse:
    """Scan for weather markets with edge. Equivalent to `cli scan`."""
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
def run_sim(
    sim: Annotated[Simulator, Depends(get_simulator)],
) -> JSONResponse:
    """Run full simulation: scan + execute paper trades. Equivalent to `cli sim`."""
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
async def run_sim_execute(
    request: Request,
    sim: Annotated[Simulator, Depends(get_simulator)],
) -> JSONResponse:
    """Execute paper trades for selected market IDs only (bet slip confirm).

    Accepts JSON body with {"market_ids": ["id1", "id2", ...]}.
    Scans markets, filters signals to only the selected IDs, and executes.
    """
    body = await request.json()
    market_ids: list[str] = body.get("market_ids", [])
    if not market_ids:
        return JSONResponse(status_code=400, content={"error": "No market_ids provided"})

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
            "skip_reasons": sim.last_skip_reasons,
            "message": f"Placed {len(trades)} bet(s).",
        })
    except Exception as e:
        logger.error("sim_execute_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        sim.close()


@app.post("/api/resolve")
def run_resolve(
    journal: Annotated[Journal, Depends(get_journal)],
) -> JSONResponse:
    """Resolve unresolved trades against actual weather. Equivalent to `cli resolve`."""
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


# ── Data Queries ────────────────────────────────────

@app.get("/api/report")
def get_report(
    days: int = 30,
    journal: Journal = Depends(get_journal),  # noqa: B008
) -> JSONResponse:
    """Get report data. Equivalent to `cli report --days N`."""
    try:
        data = journal.get_report_data(days)
        return _json(data)
    finally:
        journal.close()


@app.get("/api/portfolio")
def get_portfolio(
    settings: Annotated[Settings, Depends(get_settings)],
    journal: Annotated[Journal, Depends(get_journal)],
) -> JSONResponse:
    """Get computed portfolio state from trade history, including P&L estimates."""
    try:
        summary = journal.get_portfolio_summary(Decimal(str(settings.max_bankroll)))
        pnl_data = journal.get_open_positions_with_pnl()
        pnl_summary = pnl_data["summary"]
        summary["estimated_max_profit"] = pnl_summary["total_max_profit"]
        summary["estimated_max_loss"] = pnl_summary["total_max_loss"]
        summary["estimated_expected_pnl"] = pnl_summary["total_expected_pnl"]
        return _json(summary)
    except Exception as e:
        logger.error("portfolio_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        journal.close()


@app.get("/api/positions")
def get_positions(
    journal: Annotated[Journal, Depends(get_journal)],
) -> JSONResponse:
    """Get open positions with per-trade and aggregate P&L estimates."""
    try:
        data = journal.get_open_positions_with_pnl()
        return _json(data)
    except Exception as e:
        logger.error("positions_failed", error=str(e))
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        journal.close()


@app.get("/api/trades/{trade_id}")
def get_trade_detail(
    trade_id: str,
    journal: Annotated[Journal, Depends(get_journal)],
) -> JSONResponse:
    """Get a single trade with full market context."""
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
    journal: Journal = Depends(get_journal),  # noqa: B008
) -> JSONResponse:
    """Get trade history with market context and lifecycle state."""
    try:
        trades = journal.get_trades_with_context(days, status, outcome)
        return _json({
            "trades": trades,
            "count": len(trades),
        })
    finally:
        journal.close()


@app.get("/api/snapshots")
def get_snapshots(
    days: int = 60,
    journal: Journal = Depends(get_journal),  # noqa: B008
) -> JSONResponse:
    """Get daily portfolio snapshots for charting."""
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
