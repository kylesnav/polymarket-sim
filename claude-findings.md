# Claude Agent Findings: Weather Edge Tracker Refactor

## Summary

Refactored the Polymarket weather bot from a developer-facing "Admin Panel" into a usable "Weather Edge Tracker" with educational content, tab-based navigation, backtest-in-UI, activity logging, and a `serve` CLI command. All 69 existing tests pass with zero regressions.

---

## Files Modified

### 1. `admin-panel.html` (complete rewrite)

**Before:** A flat vertical dashboard with jargon-heavy labels, no navigation, no onboarding, no backtest, no logs. Title said "Admin Panel". First-time users saw "--" everywhere. Settings were in a modal.

**After:**

- **Tab-based navigation** with 8 tabs: Dashboard, Scan Markets, Run Simulation, Backtest, Trade History, Activity Log, Settings, How It Works
- **Onboarding card** on first launch explaining what the bot does in plain English, with step-by-step guidance and buttons linking to relevant tabs
- **Tooltip system** (`?` icons) on every technical term — hover to see plain-English explanations of Edge, Kelly Fraction, NOAA Probability, Position Cap, etc.
- **Scan tab** shows weather questions (e.g., "Will NYC high exceed 75F?") instead of hex market IDs. Results persist in localStorage across page reloads.
- **Simulate tab** with Run Simulation + Resolve Past Bets buttons, results displayed inline
- **Backtest tab** with configurable parameters (lookback days, price offset, bankroll), summary stats, caveat banner, and full trade results table
- **Activity Log tab** with real-time log polling (3s interval), level filtering, clear button, auto-scroll toggle
- **Settings promoted** from modal to full tab with educational tooltips on every field
- **"How It Works" tab** with:
  - Strategy overview explaining the NOAA forecast edge
  - Visual 5-step flow diagram (HTML/CSS, no images)
  - Quarter-Kelly bet sizing explanation with a concrete example
  - Complete safety rules table
  - Full glossary with 17 terms defined in plain English
- **Renamed** from "Admin Panel" / "Polymarket Weather Bot" to "Weather Edge Tracker"
- **Column renames** throughout: "Mkt Price" → "Market Price", "NOAA Prob" → "Forecast Prob.", "Rec. Size" → "Rec. Bet", "Size" → "Bet Size"

### 2. `src/server.py` (significant additions)

- **`POST /api/backtest`** — New endpoint accepting `{ lookback_days, price_offset_days, bankroll }`, wraps `Backtester.run()`, returns trades + summary stats + caveat + win_rate
- **`GET /api/logs`** — New endpoint with cursor-based polling. Returns log entries from an in-memory ring buffer (500 entries max)
- **Log buffer infrastructure** — Custom structlog processor that copies every log entry to a thread-safe ring buffer with sequential IDs for cursor-based polling
- **Enriched `/api/scan` and `/api/sim` responses** — Each signal now includes `question`, `location`, `event_date`, `metric`, `threshold` from the WeatherMarket model so the UI can show human-readable market descriptions
- **`_enrich_signals()` helper** — Shared function that joins Signal data with WeatherMarket metadata using `sim.last_markets`
- **Renamed** FastAPI title to "Weather Edge Tracker"

### 3. `src/cli.py` (new command)

- **`serve` command** — `uv run python -m src.cli serve [--host] [--port]` starts the web dashboard. No more needing to know `uvicorn src.server:app` incantation.

### 4. `src/simulator.py` (minor addition)

- **`last_markets` property** — Public accessor for `_last_markets` so `server.py` can enrich API responses with market metadata. Follows existing pattern of `get_portfolio()`.

---

## Files NOT Modified (during UI refactor)

`models.py`, `strategy.py`, `sizing.py`, `limits.py`, `noaa.py`, `polymarket.py`, `backtest.py`, `resolver.py`, `pyproject.toml`.

The core strategy and sizing logic is untouched. Subsequent changes to `journal.py`, `simulator.py`, `config.py`, and `test_simulator.py` were made for the double-down feature (see below).

---

## Key Design Decisions

1. **Single HTML file preserved.** The UI was already a single `admin-panel.html` and there's no build system. Adding a bundler would be over-engineering. The file grew from ~1,056 to ~1,800 lines but remains maintainable with clear section comments.

2. **Tabs over pages.** A single-page tab system avoids routing complexity while giving each feature its own dedicated space. The old design crammed everything into one scrolling page.

3. **Educational content inline, not in docs.** Users who "know nothing about Polymarket or Kelly" won't read a README. Tooltips and the "How It Works" tab put explanations exactly where the user needs them.

4. **Log buffer in memory, not on disk.** A 500-entry ring buffer with cursor-based polling is simpler than SSE/WebSockets and sufficient for the use case. Logs are ephemeral — the journal has persistent data.

5. **Backtest endpoint is async.** Since it reads the request body, it must be `async def`. The actual `Backtester.run()` is synchronous (makes real HTTP calls to NOAA/Polymarket) so FastAPI runs it in a thread pool.

---

## Verification

- **All 186 tests pass** (pytest, ~10s)
- Server starts with `uv run python -m src.cli serve`
- First-time users see onboarding card with clear instructions
- All operations (scan, simulate, resolve, backtest) available through UI
- Settings changes persist to .env
- Activity log updates in real time during operations
- Kill switch toggles from both topbar and settings tab
- Scan results persist across page reloads via localStorage

---

## Post-Refactor: Double-Down & Position Cap Changes

### Problem
With only ~4 active weather markets and a 5% per-position cap ($25/market), only $100 of the $500 bankroll could be deployed. The simulator had a hard binary gate: any existing trade for a market = skip entirely. No concept of adding to positions.

### Changes Made

1. **`src/journal.py`** — Added `get_open_position_size(market_id)` method. Returns total $ deployed on a market (sum of sizes for pending/filled trades), or zero if none.

2. **`src/simulator.py`** — Replaced the hard duplicate-market skip in `execute_signals()` with position-aware logic:
   - Calculates `remaining_room = position_cap - existing_size` for each market
   - If no room left (fully capped), skips with a clear reason message
   - If room remains, allows additional trades capped to remaining room
   - Logs whether each trade is a double-down and total position size after fill

3. **`src/config.py`** — Raised `POSITION_CAP_PCT` validator ceiling from 20% to 50%.

4. **`.env` / `.env.example`** — Bumped `POSITION_CAP_PCT` from 5% to 25% (each market can hold up to $125 on a $500 bankroll).

5. **`tests/test_simulator.py`** — Updated duplicate-skip test to test position-full behavior. Added two new tests:
   - `test_double_down_with_remaining_room` — signal capped to remaining room
   - `test_double_down_fits_within_room` — signal fits under cap, no capping needed

### Verification
- All 186 tests pass (pytest, ~10s)
- No changes to strategy, sizing, limits, or resolution logic

---

## What's Next (for V1 / real deployment)

1. Add Polymarket API key configuration in the Settings tab
2. Add a "Go Live" toggle that switches from simulation to real trading
3. Add automated scheduling (run scan + sim on a cron)
4. Add email/webhook alerts for signals above a certain confidence
5. Track individual position mark-to-market (currently simplified to cash-only)
