# Polymarket Weather Bot

Paper-trades weather contracts on [Polymarket](https://polymarket.com) by spotting mispricings between market odds and [NOAA](https://www.weather.gov/) government forecasts. No real money, no live trading — just simulation to prove the edge before risking anything.

## Quick Start

```bash
git clone https://github.com/kylesnav/polymarket-sim.git
cd polymarket-sim
uv sync
cp .env.example .env   # Edit with your Polymarket API keys (optional for simulation)
uv run python -m src.cli scan   # Scan for mispriced weather markets
```

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/). Polymarket API keys are optional — the bot uses the public Gamma API for market discovery.

## Why This Works

NOAA temperature forecasts are 85–90% accurate at 1–2 day horizons. Polymarket's weather markets are thin and driven by casual bettors who don't check NOAA data. When NOAA says there's an 80% chance NYC hits 75°F tomorrow but the market is pricing YES at $0.55, that's a 25-cent edge.

The bot finds these gaps, sizes positions conservatively (quarter-Kelly), and logs everything to a SQLite database so you can track whether the edge holds up over time.

## Setup

**Requirements:** Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url> && cd polymarket-weather-bot
uv sync
cp .env.example .env
```

Polymarket API keys in `.env` are optional — the bot uses their public Gamma API to discover markets. The trading parameters all have safe defaults.

## Daily Workflow

The bot has a three-phase lifecycle: **scan**, **simulate**, and **resolve**.

### 1. Scan for opportunities

```bash
uv run python -m src.cli scan
```

This pulls all active weather markets from Polymarket (temperature, precipitation, snowfall), fetches the NOAA forecast for each one, and prints any where the gap between NOAA's probability and the market price exceeds your edge threshold (default 10%).

```
Market                                        NOAA  Market     Edge   Size ($)
------------------------------------------------------------------------------
0x3a8f...nyc-high-temp-feb-20                  0.82    0.55   +0.27     $6.25
0x9c2d...chi-precip-feb-21                     0.35    0.60   -0.25     $5.80

Found 2 signal(s) above threshold.
```

A positive edge means buy YES (NOAA thinks it's more likely than the market). A negative edge means buy NO.

### 2. Run the simulation

```bash
uv run python -m src.cli sim
# or with a custom bankroll:
uv run python -m src.cli sim --bankroll 1000
```

This does everything `scan` does, plus executes paper trades. For each signal:
- Sizes the position using quarter-Kelly (conservative — never bets more than the math says)
- Caps total exposure per market at `POSITION_CAP_PCT` of bankroll (default 25%). If you already have a position, it adds to it up to the cap (double-down).
- Checks the daily loss limit (halts if you're down 5% for the day)
- Logs the trade intent to SQLite *before* recording the fill (if the log fails, the trade doesn't happen)

```
Found 2 signal(s) above threshold.
Executed 2 paper trade(s).
  YES 0x3a8f...nyc-high-temp-feb-2 @ 0.55 | size: $6.25 | edge: +0.27
  NO  0x9c2d...chi-precip-feb-21   @ 0.60 | size: $5.80 | edge: -0.25

Daily P&L: $+3.19 (sim)
Bankroll: $487.95
```

Run this once or twice a day. Weather markets are slow-moving — there's no benefit to scanning every hour.

### 3. Resolve past trades

```bash
uv run python -m src.cli resolve
```

After an event date passes, this checks what actually happened. It fetches the real NOAA weather data for each unresolved trade, compares it to the market threshold, and records whether you won or lost with actual P&L.

```
=== Trade Resolution Summary ===
Trades resolved: 4
Wins: 3
Losses: 1
Total actual P&L: $+8.42
```

Run this periodically to settle trades from previous days. Trades can only be resolved after their event date has passed and NOAA has published observed data.

### 4. Check your track record

```bash
uv run python -m src.cli report
# or for a longer window:
uv run python -m src.cli report --days 60
```

Shows two P&L numbers:
- **Simulated P&L** — estimated from edge at time of trade (available immediately)
- **Actual P&L** — based on resolved trades against real weather outcomes (the number that matters)

```
=== 30-day Paper Trading Report ===
Trades executed: 47
Trades resolved: 38

Simulated P&L (edge-based):
  P&L: $+42.15
  Wins: 31 | Losses: 16 | Win rate: 66%

Actual P&L (resolved trades):
  P&L: $+35.80
  Wins: 26 | Losses: 12 | Win rate: 68%

Avg edge: 14.2% | Avg position: $6.50
```

### 5. Check configuration

```bash
uv run python -m src.cli status
```

Prints current settings and whether the kill switch is engaged.

### 6. Web dashboard

```bash
uv run python -m src.cli serve
```

Opens the Weather Edge Tracker at http://127.0.0.1:8000. Everything available in the CLI is also available through the web interface — scan, simulate, resolve, view trades, adjust settings, and toggle the kill switch. The dashboard includes tooltips and a "How It Works" tab for onboarding.

## Configuration

Edit `.env` to adjust trading parameters:

| Variable | Default | What it does |
|---|---|---|
| `MAX_BANKROLL` | `500` | Hard cap on total portfolio value. Trades rejected if they'd exceed this. |
| `POSITION_CAP_PCT` | `0.25` | Max total exposure per market as fraction of bankroll (default 25% = $125 on a $500 bankroll). Supports double-downs up to the cap. Configurable 0–50%. |
| `KELLY_FRACTION` | `0.25` | Quarter-Kelly sizing. Lower = more conservative. |
| `MIN_EDGE_THRESHOLD` | `0.10` | Ignore signals with less than 10% gap between NOAA and market. |
| `DAILY_LOSS_LIMIT_PCT` | `0.05` | Stop trading for the day if sim P&L drops 5%. |
| `KILL_SWITCH` | `false` | Set `true` to immediately halt all scanning and trading. |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` to see every API call and decision. |

Polymarket API keys (`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`) are optional for V0. The bot uses Polymarket's public Gamma API for market discovery.

## How Sizing Works

The bot uses **quarter-Kelly** criterion, which means it bets 25% of what the Kelly formula recommends. Full Kelly maximizes long-run growth but is too volatile — quarter-Kelly sacrifices some growth for much smoother results.

Example: NOAA says 80% probability, market price is $0.55.
- Edge = 0.80 - 0.55 = 0.25 (25 percentage points)
- Full Kelly fraction = (0.80 - 0.55) / (1 - 0.55) = 0.556
- Quarter-Kelly fraction = 0.556 × 0.25 = 0.139
- On a $500 bankroll: position = $500 × 0.139 = $69.44
- But position cap is 25% of bankroll ($125), so final size = $69.44

If you already have $50 deployed on this market, the bot will add up to $75 more (the remaining room under the $125 cap). This double-down behavior means the bot can increase positions on markets where the edge persists, rather than leaving cash idle.

## Where Data Lives

All trade data is stored in `data/trades.db` (SQLite). The database contains four tables:

- **trades** — every paper trade with ID, side, price, size, edge, status, and (after resolution) outcome and actual P&L
- **markets** — cached market metadata (location, coordinates, event date, metric, threshold) used during trade resolution
- **positions** — open position tracking
- **daily_snapshots** — end-of-day portfolio snapshots (cash, total value, P&L, trade count)

You can query it directly:

```bash
sqlite3 data/trades.db "SELECT side, price, size, edge, status, outcome, actual_pnl FROM trades ORDER BY timestamp DESC LIMIT 10"
```

## Supported Market Types

The bot currently handles US weather markets for:

| Type | How NOAA probability is derived |
|---|---|
| **Temperature high** | Normal distribution around NOAA point forecast (3°F std dev for 1-day, 4°F for 2-day, 5°F for 3+ day) |
| **Temperature low** | Same as high temp, using overnight low forecast |
| **Precipitation** | NOAA's probability of precipitation (PoP) used directly |
| **Snowfall** | Same as precipitation (PoP-based) |

Location parsing covers ~25 major US cities (NYC, LA, Chicago, Miami, etc.). Markets for unsupported cities are skipped.

## Development

```bash
uv run pytest tests/ -v        # run tests
uv run ruff check src/         # lint
uv run pyright src/            # type check
```

## In Practice

A typical session looks like this. Copy-paste these into your terminal.

```bash
# First time only — install and configure
git clone <repo-url> && cd polymarket-weather-bot
uv sync
cp .env.example .env

# Morning: scan for opportunities and paper-trade them
uv run python -m src.cli sim

# A few days later: resolve trades whose event dates have passed
uv run python -m src.cli resolve

# Check how you're doing
uv run python -m src.cli report

# Want to see signals without trading? Just scan.
uv run python -m src.cli scan

# Debugging? Crank up log verbosity in .env:
#   LOG_LEVEL=DEBUG
# Then run again:
uv run python -m src.cli sim

# Something going wrong? Engage the kill switch:
#   KILL_SWITCH=true
# Verify it's engaged:
uv run python -m src.cli status

# Poke around the database directly
sqlite3 data/trades.db "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5"
sqlite3 data/trades.db "SELECT * FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 7"
```

If you're running this daily, the cadence is:
1. `sim` once in the morning (scans markets + places paper trades)
2. `resolve` every few days (settles trades after their event dates pass)
3. `report` whenever you want to check P&L

That's it. No daemons, no scheduler, no background processes. Or just run `uv run python -m src.cli serve` and do everything from the web dashboard.

## Troubleshooting

### API key / configuration errors

- **`ValidationError` on startup**: The bot validates all `.env` values with Pydantic. Check that numeric values are in range (e.g., `KELLY_FRACTION` must be between 0 and 1, `POSITION_CAP_PCT` between 0 and 0.5). Run `uv run python -m src.cli status` to verify your current configuration.
- **Polymarket API keys not working**: API keys (`POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`) are optional for V0 simulation. The bot uses Polymarket's public Gamma API for market discovery, so you can leave them as `...` in `.env`.
- **`.env` file not found**: Make sure you copied the example file: `cp .env.example .env`. The bot loads settings from `.env` in the project root.

### Rate limiting

- **NOAA API returning 429 or 503 errors**: The bot has a built-in token bucket rate limiter (10 requests/sec for NOAA, 5 requests/sec for Polymarket) with automatic retry and exponential backoff (3 attempts at 1s/2s/4s intervals). If you still hit limits, wait a few minutes and try again. NOAA's `api.weather.gov` can be slow or intermittent — this is normal.
- **Polymarket Gamma API errors**: The Gamma API is rate-limited. The bot retries automatically with backoff. If errors persist, check [Polymarket's status page](https://polymarket.com) or try again later.
- **Timeouts**: Both NOAA and Polymarket clients use a 30-second timeout. If you're on a slow connection, requests may time out before retries are exhausted.

### Database / storage errors

- **`sqlite3.OperationalError: unable to open database file`**: The bot stores trades in `data/trades.db`. Make sure the `data/` directory exists (it should be created automatically, but you can run `mkdir -p data` if needed).
- **Database locked errors**: SQLite doesn't support concurrent writes well. Avoid running multiple bot instances simultaneously (e.g., `sim` and `resolve` at the same time).
- **Corrupt database**: If `trades.db` becomes corrupted, you can safely delete it — `rm data/trades.db` — and the bot will create a fresh one on next run. You'll lose historical trade data.

### Missing dependencies

- **`ModuleNotFoundError`**: Run `uv sync` to install all dependencies. Make sure you're using Python 3.12+ (`python --version`).
- **`py-clob-client` import errors**: This is Polymarket's official CLOB client. It's untyped, so pyright may show warnings — these are expected and suppressed in the project config.
- **uv not found**: Install uv first: `curl -LsSf https://astral.sh/uv/install.sh | sh` (see [uv docs](https://docs.astral.sh/uv/)).

### No markets or signals found

- **`Found 0 signal(s)`**: This is normal — it means no weather markets currently have an edge above your `MIN_EDGE_THRESHOLD` (default 10%). Lower the threshold in `.env` if you want to see more signals, but smaller edges are less reliable.
- **Markets for unsupported cities**: The bot only supports ~60 major US cities. Markets for other locations are silently skipped. Check `src/polymarket.py` for the full `CITY_COORDS` list.
- **Kill switch engaged**: If the bot refuses to scan or trade, check that `KILL_SWITCH=false` in `.env`. Run `uv run python -m src.cli status` to verify.

### Debug logging

Set `LOG_LEVEL=DEBUG` in `.env` to see every API call, parsing decision, and sizing calculation. This is the fastest way to diagnose unexpected behavior.

## What's NOT in V0

This is a simulation-only proof of concept. There is no:
- Live trading or real money at risk
- Multi-strategy support (just NOAA vs. market price)
- Cross-platform arbitrage (Polymarket only)
- LLM-based analysis
- Automated scheduling (run commands manually or via cron)

The goal is 60+ days of simulation data with consistent positive P&L before graduating to live trading.
