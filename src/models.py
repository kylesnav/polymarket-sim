"""Domain models for the Polymarket weather bot.

All models are frozen (immutable) Pydantic models with strict validation.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class WeatherMarket(BaseModel, frozen=True):
    """A Polymarket weather contract with parsed event details."""

    market_id: str
    question: str
    location: str
    lat: float
    lon: float
    event_date: date
    metric: Literal["temperature_high", "temperature_low", "precipitation", "snowfall"]
    threshold: float
    comparison: Literal["above", "below", "between"]
    yes_price: Decimal
    no_price: Decimal
    volume: Decimal
    close_date: datetime
    token_id: str = ""


class NOAAForecast(BaseModel, frozen=True):
    """Parsed NOAA forecast for a specific location and date."""

    location: str
    forecast_date: date
    retrieved_at: datetime
    temperature_high: float | None = None
    temperature_low: float | None = None
    precip_probability: float | None = None
    forecast_narrative: str = ""


class NOAAObservation(BaseModel, frozen=True):
    """Actual observed weather from a NOAA weather station."""

    station_id: str
    location: str
    observation_date: date
    retrieved_at: datetime
    temperature_high: float | None = None
    temperature_low: float | None = None
    precipitation: float | None = None  # inches


class Signal(BaseModel, frozen=True):
    """Trading signal from NOAA-vs-market comparison."""

    market_id: str
    noaa_probability: Decimal
    market_price: Decimal
    edge: Decimal
    side: Literal["YES", "NO"]
    kelly_fraction: Decimal
    recommended_size: Decimal
    confidence: Literal["high", "medium", "low"]


class Trade(BaseModel, frozen=True):
    """Executed (simulated) trade record."""

    trade_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    market_id: str
    side: Literal["YES", "NO"]
    price: Decimal
    size: Decimal
    noaa_probability: Decimal
    edge: Decimal
    timestamp: datetime
    status: Literal["pending", "filled", "resolved", "cancelled"] = "pending"
    outcome: Literal["won", "lost"] | None = None
    actual_pnl: Decimal | None = None


class Position(BaseModel, frozen=True):
    """Open position tracking."""

    market_id: str
    side: Literal["YES", "NO"]
    entry_price: Decimal
    size: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    opened_at: datetime


class Portfolio(BaseModel, frozen=True):
    """Cash + positions + daily P&L snapshot."""

    cash: Decimal
    positions: list[Position] = Field(default_factory=list)
    daily_pnl: Decimal = Decimal("0")
    total_value: Decimal = Decimal("0")
    starting_bankroll: Decimal = Decimal("500")


class BacktestTrade(BaseModel, frozen=True):
    """A fully-resolved backtest trade with actual outcome."""

    market_id: str
    question: str
    location: str
    event_date: date
    metric: str
    threshold: float
    comparison: str
    historical_price: Decimal
    noaa_probability: Decimal
    edge: Decimal
    side: Literal["YES", "NO"]
    kelly_fraction: Decimal
    size: Decimal
    actual_value: float
    condition_met: bool
    outcome: Literal["won", "lost"]
    actual_pnl: Decimal


class BacktestResult(BaseModel, frozen=True):
    """Aggregate backtest results."""

    trades: list[BacktestTrade] = Field(default_factory=list)
    wins: int = 0
    losses: int = 0
    total_pnl: Decimal = Decimal("0")
    markets_scanned: int = 0
    markets_skipped: int = 0
    caveat: str = (
        "IMPORTANT: This backtest uses actual weather observations as a proxy for "
        "NOAA forecasts. Real forecasts have error margins, so these results represent "
        "an optimistic upper bound on algorithm performance."
    )
