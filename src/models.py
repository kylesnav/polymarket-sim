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
    created_at: datetime | None = None


class NOAAForecast(BaseModel, frozen=True):
    """Parsed NOAA forecast for a specific location and date."""

    location: str
    forecast_date: date
    retrieved_at: datetime
    temperature_high: float | None = None
    temperature_low: float | None = None
    precip_probability: float | None = None
    forecast_narrative: str = ""
    update_time: datetime | None = None


class NOAAObservation(BaseModel, frozen=True):
    """Actual observed weather from a NOAA weather station."""

    station_id: str
    location: str
    observation_date: date
    retrieved_at: datetime
    temperature_high: float | None = None
    temperature_low: float | None = None
    precipitation: float | None = None  # inches


class NBMPercentiles(BaseModel, frozen=True):
    """NBM probabilistic temperature percentiles for a station."""

    station_id: str
    forecast_date: date
    retrieved_at: datetime
    p10: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    std_dev: float | None = None
    metric: Literal["temperature_high", "temperature_low"]


class OutcomeBucket(BaseModel, frozen=True):
    """A single outcome bucket within a multi-outcome weather event."""

    token_id: str
    condition_id: str
    outcome_label: str
    lower_bound: float | None = None  # None for "X or below" buckets
    upper_bound: float | None = None  # None for "X or above" buckets
    yes_price: Decimal
    no_price: Decimal
    volume: Decimal


class WeatherEvent(BaseModel, frozen=True):
    """A multi-outcome Polymarket weather event with N buckets."""

    event_id: str
    question: str
    location: str
    lat: float
    lon: float
    event_date: date
    metric: Literal["temperature_high", "temperature_low", "precipitation", "snowfall"]
    buckets: list[OutcomeBucket] = Field(default_factory=list)
    close_date: datetime
    created_at: datetime | None = None


class OrderBookLevel(BaseModel, frozen=True):
    """A single price level in the order book."""

    price: Decimal
    size: Decimal


class OrderBook(BaseModel, frozen=True):
    """L2 order book snapshot for a token."""

    token_id: str
    bids: list[OrderBookLevel] = Field(default_factory=list)  # Descending by price
    asks: list[OrderBookLevel] = Field(default_factory=list)  # Ascending by price
    timestamp: datetime


class ProbabilityDistribution(BaseModel, frozen=True):
    """NOAA-derived probability distribution across event buckets."""

    event_id: str
    bucket_probabilities: list[Decimal]
    mean_forecast: float
    std_dev: float
    source: Literal["nbm", "point_forecast_normal", "fallback"]


class BucketSignal(BaseModel, frozen=True):
    """Trading signal for a specific bucket within a multi-outcome event."""

    event_id: str
    bucket_index: int
    token_id: str
    condition_id: str
    outcome_label: str
    noaa_probability: Decimal
    market_price: Decimal
    edge: Decimal
    side: Literal["YES", "NO"]
    kelly_fraction: Decimal
    recommended_size: Decimal
    confidence: Literal["high", "medium", "low"]
    forecast_horizon_days: int = 0


class Signal(BaseModel, frozen=True):
    """Trading signal from NOAA-vs-market comparison (legacy binary model)."""

    market_id: str
    noaa_probability: Decimal
    market_price: Decimal
    edge: Decimal
    side: Literal["YES", "NO"]
    kelly_fraction: Decimal
    recommended_size: Decimal
    confidence: Literal["high", "medium", "low"]
    forecast_horizon_days: int = 0


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
    event_id: str = ""
    bucket_index: int = -1
    token_id: str = ""
    outcome_label: str = ""
    fill_price: Decimal | None = None
    book_depth_at_signal: Decimal | None = None
    resolution_source: str = ""


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
