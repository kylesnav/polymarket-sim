## What This Is

A Python bot that finds mispriced weather contracts on Polymarket by comparing market prices against NOAA forecast data. Simulation only — no live trading.

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

1. **Scan** Polymarket for active weather markets (temperature, precipitation, snowfall)
2. **Parse** the market question to extract: location, date, threshold
3. **Fetch** NOAA forecast for that location + date from `api.weather.gov`
4. **Convert** NOAA forecast into a probability estimate
5. **Compare** NOAA probability vs Polymarket price
6. **Signal** if absolute discrepancy > threshold (default: 10 percentage points)
7. **Size** using quarter-Kelly: `f* = 0.25 × (p_noaa - p_market) / (1 - p_market)`
8. **Check** all limits (bankroll, position cap, daily loss)
9. **Execute** paper trade in simulator and log to SQLite

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

---

## Coding Standards

- **Type hints on everything.** `pyright --strict` must pass.
- **Frozen Pydantic models** for all domain objects.
- **Google-style docstrings** on all public functions.
- **No print statements.** `structlog` only.
- **No `Any`** except wrapping `py-clob-client` return types.
- **Retry logic** on all external API calls: 3 attempts, exponential backoff (1s/2s/4s).
