"""Application settings loaded from environment variables."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Bot configuration loaded from .env file.

    Validates all trading parameters are within safe ranges.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Polymarket credentials (optional for V0 simulation)
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""

    # Trading config
    max_bankroll: float = 500.0
    position_cap_pct: float = 0.05
    kelly_fraction: float = 0.25
    daily_loss_limit_pct: float = 0.05
    min_edge_threshold: float = 0.10
    kill_switch: bool = False

    # Logging
    log_level: str = "INFO"

    @field_validator("max_bankroll")
    @classmethod
    def bankroll_positive(cls, v: float) -> float:
        """Validate bankroll is positive."""
        if v <= 0:
            msg = "MAX_BANKROLL must be > 0"
            raise ValueError(msg)
        return v

    @field_validator("kelly_fraction")
    @classmethod
    def kelly_in_range(cls, v: float) -> float:
        """Validate Kelly fraction is between 0 and 1."""
        if not 0 < v <= 1:
            msg = "KELLY_FRACTION must be between 0 (exclusive) and 1 (inclusive)"
            raise ValueError(msg)
        return v

    @field_validator("position_cap_pct")
    @classmethod
    def position_cap_in_range(cls, v: float) -> float:
        """Validate position cap is between 0 and 0.2."""
        if not 0 < v <= 0.2:
            msg = "POSITION_CAP_PCT must be between 0 (exclusive) and 0.2 (inclusive)"
            raise ValueError(msg)
        return v

    @field_validator("min_edge_threshold")
    @classmethod
    def edge_threshold_in_range(cls, v: float) -> float:
        """Validate minimum edge threshold is between 0 and 0.5."""
        if not 0 < v <= 0.5:
            msg = "MIN_EDGE_THRESHOLD must be between 0 (exclusive) and 0.5 (inclusive)"
            raise ValueError(msg)
        return v

    @field_validator("daily_loss_limit_pct")
    @classmethod
    def daily_loss_in_range(cls, v: float) -> float:
        """Validate daily loss limit is between 0 and 1."""
        if not 0 < v <= 1:
            msg = "DAILY_LOSS_LIMIT_PCT must be between 0 (exclusive) and 1 (inclusive)"
            raise ValueError(msg)
        return v
