## What This Is

A Python bot that finds mispriced weather contracts on Polymarket by comparing market prices against NOAA forecast data. Supports multi-outcome events (temperature buckets) and binary markets. Simulation only — no live trading.

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Polymarket SDK is Python-first |
| Package manager | `uv` | Fast, lockfile, no virtualenv headaches |
| Polymarket SDK | `py-clob-client` | Official CLOB client |
| HTTP | `httpx` | Modern async HTTP for NOAA calls |
| Data models | `pydantic` | Frozen models, strict validation |
| Config | `pydantic-settings` + `.env` | Type-safe env loading |
| CLI | `typer` | Minimal CLI framework |
| Web server | `FastAPI` + `uvicorn` | Dashboard API |
| Logging | `structlog` | Structured JSON logs |
| Database | SQLite via `sqlite3` | Zero-infra trade journal |
| Testing | `pytest` | Standard |
| Linting | `ruff` + `pyright` | Fast, strict |

---

## Architecture

### Multi-Outcome Pipeline (primary)

Polymarket weather events have ~9 buckets (e.g., "48-49°F", "50-51°F"). The bot:

1. **Fetch events** from Gamma API grouped by parent event (`get_weather_events()`)
2. **Parse buckets** — each market within an event becomes an `OutcomeBucket` with bounds
3. **NOAA distribution** — convert point forecast + uncertainty into per-bucket probabilities using normal CDF (`compute_bucket_distribution()`)
4. **Compare** NOAA probability vs market YES price for each bucket
5. **Signal** buckets where |edge| > threshold
6. **Size** using multi-outcome Kelly across buckets (`calculate_multi_outcome_kelly()`)
7. **Paper execute** via `PaperExecutor` — walks real order books, checks slippage (max 5%)
8. **Resolve** using Polymarket's own resolution data (`get_resolution_data()`), NOT NOAA observations

### Legacy Binary Pipeline (fallback)

For markets without event grouping, falls back to the original binary YES/NO model with `scan_weather_markets()` and NOAA-based resolution.

### Key Models

- `WeatherEvent` — multi-outcome event with `list[OutcomeBucket]`
- `OutcomeBucket` — single bucket: token_id, bounds, prices, volume
- `BucketSignal` — trading signal for one bucket within an event
- `ProbabilityDistribution` — NOAA-derived probs per bucket
- `OrderBook` / `OrderBookLevel` — L2 order book for slippage checks
- `Trade` — extended with event_id, bucket_index, token_id, fill_price, book_depth

### Resolution

Trades are resolved via Polymarket's own resolution data (Gamma API `outcomePrices`), NOT NOAA weather observations. This is critical — Polymarket resolves from Weather Underground, not NOAA. Legacy trades without event_id fall back to NOAA observations.

---

## CLI Commands

```bash
uv run python -m src scan       # Legacy binary market scan
uv run python -m src events     # Multi-outcome event scan with bucket signals
uv run python -m src sim        # Scan + execute paper trades
uv run python -m src resolve    # Resolve trades via Polymarket resolution data
uv run python -m src positions  # Show open positions with P&L estimates
uv run python -m src report     # Paper P&L summary
uv run python -m src status     # Config and safety rail status
uv run python -m src serve      # Start web dashboard (default: localhost:8000)
```

---

## API Endpoints

### Multi-Outcome (new)
- `POST /api/events/scan` — scan events with bucket-level signals and NOAA distribution
- `POST /api/events/execute` — execute selected bucket signals (bet slip)
- `GET /api/events/{event_id}` — detailed event view with trade history

### Core
- `POST /api/scan` — legacy binary scan
- `POST /api/sim` — legacy scan + execute
- `POST /api/sim/execute` — selective execute by market_id
- `POST /api/resolve` — resolve trades (Polymarket-sourced)
- `GET /api/status` — config + lifecycle counts
- `PUT /api/settings` — update .env config
- `PUT /api/kill-switch` — toggle kill switch

### Data
- `GET /api/portfolio` — portfolio summary with P&L estimates
- `GET /api/positions` — open positions with per-trade P&L
- `GET /api/trades` — trade history (filterable by status/outcome)
- `GET /api/trades/{trade_id}` — single trade detail
- `GET /api/report?days=N` — paper P&L report
- `GET /api/snapshots?days=N` — daily portfolio snapshots
- `GET /api/logs?since=N` — structured log entries

---

## Safety Rails

Non-negotiable. Code must enforce all of these.

1. **Simulation only.** No live trading code path.
2. **Hard bankroll ceiling.** `MAX_BANKROLL` set in `.env`. Simulator refuses trades that would exceed it.
3. **Per-position cap.** `POSITION_CAP_PCT` of bankroll (default 25%, configurable up to 50%).
4. **Quarter-Kelly only.** Kelly fraction × 0.25, always.
5. **Minimum edge threshold.** Don't trade unless NOAA-vs-market gap >= 10% (configurable).
6. **Daily loss limit: -5%.** If sim P&L drops 5% intraday, halt for the day.
7. **Log before execute.** Trade intent logged to SQLite before simulator records the fill.
8. **Kill switch.** `KILL_SWITCH=true` in `.env` halts everything.
9. **Order book depth check.** PaperExecutor rejects trades where book can't fill at < 5% slippage.

---

## Frontend

The dashboard (`admin-panel.html`) uses the **delightful-design-system** — a neo-brutalist design with warm cream backgrounds, OKLCH colors, Inter + JetBrains Mono fonts, thick borders, and solid zero-blur shadows.

5 tabs: Dashboard, Markets (with bucket distribution chart), Portfolio, Activity, Settings.

Light/dark theme toggle via `data-theme` attribute on `<html>`.

---

## Coding Standards

- **Type hints on everything.** `pyright --strict` must pass.
- **Frozen Pydantic models** for all domain objects.
- **Google-style docstrings** on all public functions.
- **No print statements.** `structlog` only.
- **No `Any`** except wrapping `py-clob-client` return types.
- **Retry logic** on all external API calls: 3 attempts, exponential backoff (1s/2s/4s).
