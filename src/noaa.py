"""NOAA Weather API client using httpx.

Fetches forecasts from api.weather.gov for weather market comparison.
Two-step flow: /points/{lat},{lon} → grid metadata → /gridpoints/{office}/{x},{y}/forecast
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from typing import Any

import httpx
import structlog

from src.models import NOAAForecast

logger = structlog.get_logger()

NOAA_BASE_URL = "https://api.weather.gov"
USER_AGENT = "polymarket-weather-bot/0.1.0 (weather-simulation)"


class NOAAClient:
    """Client for the NOAA Weather API.

    Caches grid lookups since they never change for a given lat/lon.
    """

    def __init__(self) -> None:
        """Initialize the NOAA client with httpx and grid cache."""
        self._http = httpx.Client(
            base_url=NOAA_BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
            timeout=30.0,
        )
        self._grid_cache: dict[str, tuple[str, int, int]] = {}
        logger.info("noaa_client_initialized")

    def close(self) -> None:
        """Close the HTTP client."""
        self._http.close()

    def get_forecast(self, lat: float, lon: float, target_date: date) -> NOAAForecast | None:
        """Fetch NOAA forecast for a location and target date.

        Args:
            lat: Latitude of the location.
            lon: Longitude of the location.
            target_date: The date to get the forecast for.

        Returns:
            NOAAForecast if successful, None if the forecast cannot be retrieved.
        """
        grid = self._get_grid_info(lat, lon)
        if grid is None:
            return None

        office, grid_x, grid_y = grid

        forecast_data = self._fetch_forecast(office, grid_x, grid_y)
        if forecast_data is None:
            return None

        return self._parse_forecast(forecast_data, lat, lon, target_date)

    def _get_grid_info(self, lat: float, lon: float) -> tuple[str, int, int] | None:
        """Get grid office and coordinates for a lat/lon.

        Results are cached since grid data never changes.

        Args:
            lat: Latitude.
            lon: Longitude.

        Returns:
            Tuple of (office, grid_x, grid_y) or None on failure.
        """
        cache_key = f"{lat:.4f},{lon:.4f}"
        if cache_key in self._grid_cache:
            logger.debug("grid_cache_hit", cache_key=cache_key)
            return self._grid_cache[cache_key]

        logger.info("fetching_grid_info", lat=lat, lon=lon)
        response = self._request_with_retry(f"/points/{lat},{lon}")
        if response is None:
            return None

        try:
            props: dict[str, Any] = response.get("properties", {})
            if not isinstance(props, dict):
                return None
            office = str(props.get("gridId", ""))
            grid_x = int(props.get("gridX", 0))
            grid_y = int(props.get("gridY", 0))

            if not office:
                logger.error("no_grid_office", response=response)
                return None

            self._grid_cache[cache_key] = (office, grid_x, grid_y)
            logger.info("grid_info_cached", office=office, grid_x=grid_x, grid_y=grid_y)
            return office, grid_x, grid_y
        except (KeyError, TypeError, ValueError) as e:
            logger.error("grid_parse_error", error=str(e))
            return None

    def _fetch_forecast(
        self, office: str, grid_x: int, grid_y: int
    ) -> dict[str, Any] | None:
        """Fetch the 7-day forecast for a grid point.

        Args:
            office: NWS office ID (e.g., "OKX").
            grid_x: Grid X coordinate.
            grid_y: Grid Y coordinate.

        Returns:
            Forecast JSON dict or None on failure.
        """
        logger.info("fetching_forecast", office=office, grid_x=grid_x, grid_y=grid_y)
        return self._request_with_retry(f"/gridpoints/{office}/{grid_x},{grid_y}/forecast")

    def _parse_forecast(
        self,
        data: dict[str, Any],
        lat: float,
        lon: float,
        target_date: date,
    ) -> NOAAForecast | None:
        """Parse NOAA forecast JSON into a NOAAForecast model.

        Finds the forecast period matching the target date and extracts
        temperature and precipitation data.

        Args:
            data: Raw forecast JSON.
            lat: Latitude for location name.
            lon: Longitude for location name.
            target_date: Date to find forecast for.

        Returns:
            NOAAForecast or None if target date not found.
        """
        props: dict[str, Any] = data.get("properties", {})
        if not isinstance(props, dict):
            return None

        periods: list[dict[str, Any]] = props.get("periods", [])
        if not isinstance(periods, list):
            return None

        temp_high: float | None = None
        temp_low: float | None = None
        precip_prob: float | None = None
        narrative = ""

        for period in periods:
            if not isinstance(period, dict):
                continue

            start_time: str = str(period.get("startTime", ""))
            if not start_time:
                continue

            try:
                period_date = datetime.fromisoformat(start_time).date()
            except ValueError:
                continue

            if period_date != target_date:
                continue

            # Extract temperature
            temperature: Any = period.get("temperature")
            if isinstance(temperature, int | float):
                is_daytime: bool = bool(period.get("isDaytime", True))
                if is_daytime:
                    temp_high = float(temperature)
                else:
                    temp_low = float(temperature)

            # Extract precipitation probability
            precip_data: Any = period.get("probabilityOfPrecipitation")
            if isinstance(precip_data, dict):
                value: Any = precip_data.get("value")
                if isinstance(value, int | float):
                    precip_prob = float(value) / 100.0  # Convert percentage to 0-1

            # Grab narrative
            detailed: str = str(period.get("detailedForecast", ""))
            if detailed:
                if narrative:
                    narrative += " | "
                narrative += detailed

        if temp_high is None and temp_low is None and precip_prob is None:
            logger.warning("no_forecast_data_for_date", target_date=str(target_date))
            return None

        location = f"{lat:.2f},{lon:.2f}"
        return NOAAForecast(
            location=location,
            forecast_date=target_date,
            retrieved_at=datetime.now(tz=UTC),
            temperature_high=temp_high,
            temperature_low=temp_low,
            precip_probability=precip_prob,
            forecast_narrative=narrative,
        )

    def _request_with_retry(
        self,
        path: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> dict[str, Any] | None:
        """Make an HTTP GET request with exponential backoff retry.

        Args:
            path: URL path relative to NOAA base URL.
            max_retries: Maximum retry attempts.
            base_delay: Base delay in seconds.

        Returns:
            Parsed JSON dict or None on failure.
        """
        for attempt in range(max_retries):
            try:
                response = self._http.get(path)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "noaa_request_retry",
                    path=path,
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    delay=delay,
                    error=str(e),
                )
                if attempt < max_retries - 1:
                    time.sleep(delay)

        logger.error("noaa_request_failed", path=path, max_retries=max_retries)
        return None
