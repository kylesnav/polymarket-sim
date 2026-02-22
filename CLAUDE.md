## What This Is

A Python bot that finds mispriced weather contracts on Polymarket by comparing market prices against NOAA forecast data. It runs in simulation mode, tracking paper P&L against real market data. No live trading, no Kalshi, no LLM ensemble, no arbitrage — just one clean edge.

**The edge:** NOAA forecasts are 85–90% accurate at 1–2 day horizons. Casual bettors on Polymarket consistently misprice weather contracts relative to these freely available government forecasts, especially for temperature and precipitation markets. Documented examples show $1K → $24K returns exploiting this gap.

**Budget:** $500/month additions. Simulation until paper P&L proves consistent edge over 60+ days.

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
| Logging | `structlog` | Structured JSON logs |
| Database | SQLite via `sqlite3` | Zero-infra trade journal |
| Testing | `pytest` | Standard |
| Linting | `ruff` + `pyright` | Fast, strict |

### NOT using (yet)
- `asyncio` / `aiohttp` — synchronous is fine for 10–20 market checks/day
- `APScheduler` — use system cron or manual runs initially
- `anthropic` / `openai` — no LLM calls in V0; pure data comparison
- `kalshi-python` — Polymarket only
- `pandas` / `polars` — stdlib is enough for this scope

---

## Architecture

```
polymarket-weather-bot/
├── CLAUDE.md
├── pyproject.toml
├── .env.example
├── src/
│   ├── __init__.py
│   ├── config.py          # Settings from .env
│   ├── models.py          # Market, Signal, Trade, Position
│   ├── polymarket.py      # Polymarket API wrapper
│   ├── noaa.py            # NOAA weather API client
│   ├── strategy.py        # NOAA vs market price comparison
│   ├── sizing.py          # Quarter-Kelly position sizing
│   ├── limits.py          # Bankroll/position hard limits
│   ├── simulator.py       # Paper trading engine (supports double-downs)
│   ├── journal.py         # SQLite trade log
│   ├── resolver.py        # Trade resolution against actual weather
│   ├── server.py          # FastAPI web dashboard backend
│   └── cli.py             # Typer entrypoint (scan/sim/resolve/report/status/serve)
├── admin-panel.html           # Web dashboard UI
├── tests/
│   ├── test_sizing.py
│   ├── test_limits.py
│   ├── test_strategy.py
│   ├── test_simulator.py
│   ├── test_journal.py
│   ├── test_noaa.py
│   ├── test_polymarket.py
│   ├── test_resolver.py
│   ├── test_server.py
│   └── fixtures/          # Sample NOAA + market data
└── data/
    └── trades.db
```

Flat `src/` is intentional — the codebase is small enough that subdirectories add navigation overhead for zero benefit. The V1 expansion adds `clients/`, `strategies/`, `risk/` folders when we add strategies.

---

## How the Strategy Works

```
┌─────────────┐     ┌──────────────┐     ┌───────────────┐
│  Polymarket  │     │  NOAA API    │     │  Compare      │
│  Weather     │────▶│  Forecast    │────▶│  & Signal     │
│  Markets     │     │  for same    │     │  if gap >     │
│              │     │  event       │     │  threshold    │
└─────────────┘     └──────────────┘     └───────┬───────┘
                                                  │
                                          ┌───────▼───────┐
                                          │  Quarter-Kelly │
                                          │  Sizing        │
                                          └───────┬───────┘
                                                  │
                                          ┌───────▼───────┐
                                          │  Limits Check  │
                                          │  & Sim Trade   │
                                          └───────────────┘
```

### Step by step:

1. **Scan** Polymarket for active weather markets (temperature, precipitation, snowfall)
2. **Parse** the market question to extract: location, date, threshold (e.g., "Will NYC high temp exceed 75°F on March 5?")
3. **Fetch** NOAA forecast for that location + date from `api.weather.gov`
4. **Convert** NOAA forecast into a probability estimate:
   - Temperature: use NOAA's probabilistic forecasts (they provide percentile data)
   - Precipitation: use NOAA's probability of precipitation (PoP) directly
   - If NOAA gives a point forecast + error bars, model as normal distribution
5. **Compare** NOAA probability vs Polymarket price
6. **Signal** if absolute discrepancy > threshold (default: 10 percentage points)
7. **Size** using quarter-Kelly: `f* = 0.25 × (p_noaa - p_market) / (1 - p_market)`
8. **Check** all limits (bankroll, position cap, daily loss). If a position already exists for this market, cap the new trade to remaining room under the position limit (double-down). Skip only if fully capped.
9. **Execute** paper trade in simulator and log to SQLite

### NOAA Data Sources

| Endpoint | Data | Latency |
|---|---|---|
| `api.weather.gov/points/{lat},{lon}` | Grid metadata | Static lookup |
| `api.weather.gov/gridpoints/{office}/{x},{y}/forecast` | 7-day forecast (12hr periods) | Updates every 1–6 hrs |
| `api.weather.gov/gridpoints/{office}/{x},{y}/forecast/hourly` | Hourly forecast (156 hrs) | Updates every 1 hr |

NOAA API is free — no API key, no auth, no rate limit concerns at our scale. Docs: https://www.weather.gov/documentation/services-web-api

---

## Safety Rails

Non-negotiable. Code must enforce all of these.

1. **Simulation only.** V0 has no live trading code path. Period.
2. **Hard bankroll ceiling.** `MAX_BANKROLL` set in `.env`. Simulator refuses trades that would exceed it.
3. **Per-position cap.** `POSITION_CAP_PCT` of bankroll (default 25%, configurable up to 50%). Total exposure per market (including double-downs) cannot exceed this.
4. **Quarter-Kelly only.** Kelly fraction × 0.25, always.
5. **Minimum edge threshold.** Don't trade unless NOAA-vs-market gap ≥ 10% (configurable).
6. **Daily loss limit: -5%.** If sim P&L drops 5% intraday, halt for the day.
7. **Log before execute.** Trade intent logged to SQLite before simulator records the fill. If logging fails, trade doesn't happen.
8. **Kill switch.** `KILL_SWITCH=true` in `.env` halts everything.

---

## Coding Standards

- **Type hints on everything.** `pyright --strict` must pass.
- **Frozen Pydantic models** for all domain objects.
- **Google-style docstrings** on all public functions.
- **No print statements.** `structlog` only.
- **No `Any`** except wrapping `py-clob-client` return types.
- **Retry logic** on all external API calls: 3 attempts, exponential backoff (1s/2s/4s).

---

## Data Models

```python
# Simplified — see models.py for full definitions

class WeatherMarket(BaseModel):
    """A Polymarket weather contract with parsed event details."""
    market_id: str
    question: str
    location: str          # e.g., "New York, NY"
    lat: float
    lon: float
    event_date: date
    metric: Literal["temperature_high", "temperature_low", "precipitation", "snowfall"]
    threshold: float       # e.g., 75.0 (degrees F)
    comparison: Literal["above", "below", "between"]
    yes_price: Decimal
    no_price: Decimal
    volume: Decimal
    close_date: datetime

class NOAAForecast(BaseModel):
    """Parsed NOAA forecast for a specific location and date."""
    location: str
    forecast_date: date
    retrieved_at: datetime
    temperature_high: float | None
    temperature_low: float | None
    precip_probability: float | None  # 0.0 to 1.0
    forecast_narrative: str

class Signal(BaseModel):
    """Trading signal from NOAA-vs-market comparison."""
    market_id: str
    noaa_probability: Decimal   # Our estimate from NOAA data
    market_price: Decimal       # Current YES price
    edge: Decimal               # noaa_prob - market_price
    side: Literal["YES", "NO"]
    kelly_fraction: Decimal
    recommended_size: Decimal
    confidence: Literal["high", "medium", "low"]
```

---

## Environment Variables

```env
# Polymarket
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...

# Trading config
MAX_BANKROLL=500
POSITION_CAP_PCT=0.25
KELLY_FRACTION=0.25
DAILY_LOSS_LIMIT_PCT=0.05
MIN_EDGE_THRESHOLD=0.10
KILL_SWITCH=false

# Logging
LOG_LEVEL=INFO
```

---

## CLI Commands

```bash
# Scan for weather markets with edge
uv run python -m src.cli scan

# Run simulation with $500 bankroll
uv run python -m src.cli sim --bankroll 500

# Resolve past trades against actual weather outcomes
uv run python -m src.cli resolve

# View paper P&L
uv run python -m src.cli report --days 30

# Check current config and kill switch status
uv run python -m src.cli status

# Start the web dashboard (Weather Edge Tracker)
uv run python -m src.cli serve

# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check src/ && uv run pyright src/
```

---

## Success Criteria (before graduating to V1)

- [ ] 60+ days of simulation data
- [ ] Positive paper P&L after accounting for Polymarket's zero-fee structure
- [ ] Signal accuracy: >55% of weather signals resolve correctly
- [ ] No safety rail violations in logs
- [ ] Strategy generates ≥5 actionable signals per week

**Do NOT move to V1 (multi-strategy, multi-platform, live trading) until all five criteria above are met.** The whole point of V0 is proving the edge exists before risking real money.
