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
