"""Polymarket API wrapper for weather market discovery."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
import structlog
from py_clob_client.client import ClobClient  # type: ignore[import-untyped]

from src.models import WeatherMarket

logger = structlog.get_logger()

POLYMARKET_HOST = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

WEATHER_KEYWORDS: list[str] = [
    "temperature",
    "temp",
    "high temp",
    "low temp",
    "precipitation",
    "precip",
    "snowfall",
    "snow",
    "rain",
    "weather",
    "°f",
    "°c",
    "inches of rain",
    "inches of snow",
]

# Common US city coordinates for market question parsing
CITY_COORDS: dict[str, tuple[float, float]] = {
    # Top 50 US cities by population + weather-volatile cities
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "la": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "chi": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652),
    "philly": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970),
    "miami": (25.7617, -80.1918),
    "atlanta": (33.7490, -84.3880),
    "boston": (42.3601, -71.0589),
    "seattle": (47.6062, -122.3321),
    "denver": (39.7392, -104.9903),
    "washington": (38.9072, -77.0369),
    "dc": (38.9072, -77.0369),
    "san francisco": (37.7749, -122.4194),
    "sf": (37.7749, -122.4194),
    "san fran": (37.7749, -122.4194),
    "nashville": (36.1627, -86.7816),
    "detroit": (42.3314, -83.0458),
    "minneapolis": (44.9778, -93.2650),
    "portland": (45.5152, -122.6784),
    "las vegas": (36.1699, -115.1398),
    "vegas": (36.1699, -115.1398),
    "baltimore": (39.2904, -76.6122),
    "milwaukee": (43.0389, -87.9065),
    "st. louis": (38.6270, -90.1994),
    "st louis": (38.6270, -90.1994),
    # Additional major cities
    "austin": (30.2672, -97.7431),
    "jacksonville": (30.3322, -81.6557),
    "fort worth": (32.7555, -97.3308),
    "columbus": (39.9612, -82.9988),
    "charlotte": (35.2271, -80.8431),
    "indianapolis": (39.7684, -86.1581),
    "indy": (39.7684, -86.1581),
    "san jose": (37.3382, -121.8863),
    "memphis": (35.1495, -90.0490),
    "oklahoma city": (35.4676, -97.5164),
    "okc": (35.4676, -97.5164),
    "louisville": (38.2527, -85.7585),
    "tucson": (32.2226, -110.9747),
    "el paso": (31.7619, -106.4850),
    "raleigh": (35.7796, -78.6382),
    "new orleans": (29.9511, -90.0715),
    "nola": (29.9511, -90.0715),
    "tampa": (27.9506, -82.4572),
    "orlando": (28.5384, -81.3789),
    "kansas city": (39.0997, -94.5786),
    "kc": (39.0997, -94.5786),
    "sacramento": (38.5816, -121.4944),
    "pittsburgh": (40.4406, -79.9959),
    "cincinnati": (39.1031, -84.5120),
    "cleveland": (41.4993, -81.6944),
    "omaha": (41.2565, -95.9345),
    "tulsa": (36.1540, -95.9928),
    "albuquerque": (35.0844, -106.6504),
    "honolulu": (21.3069, -157.8583),
    "anchorage": (61.2181, -149.9003),
    # Weather-volatile cities
    "buffalo": (42.8864, -78.8784),
    "rochester": (43.1566, -77.6088),
    "syracuse": (43.0481, -76.1474),
    "des moines": (41.5868, -93.6250),
    "wichita": (37.6872, -97.3301),
    "boise": (43.6150, -116.2023),
    "salt lake city": (40.7608, -111.8910),
    "slc": (40.7608, -111.8910),
    "spokane": (47.6588, -117.4260),
    "fargo": (46.8772, -96.7898),
    "sioux falls": (43.5446, -96.7311),
    "billings": (45.7833, -108.5007),
    "reno": (39.5296, -119.8138),
    "colorado springs": (38.8339, -104.8214),
    "little rock": (34.7465, -92.2896),
    "jackson": (32.2988, -90.1848),
    "birmingham": (33.5207, -86.8025),
    "richmond": (37.5407, -77.4360),
    "norfolk": (36.8508, -76.2859),
    "charleston": (32.7765, -79.9311),
    "savannah": (32.0809, -81.0912),
    "hartford": (41.7658, -72.6734),
    "providence": (41.8240, -71.4128),
    "knoxville": (35.9606, -83.9207),
}

MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MetricType = Literal["temperature_high", "temperature_low", "precipitation", "snowfall"]
ComparisonType = Literal["above", "below", "between"]


def _retry_with_backoff(
    func: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """Execute a callable with exponential backoff retry.

    Args:
        func: Callable to execute (wraps py-clob-client methods).
        max_retries: Maximum number of retry attempts.
        base_delay: Base delay in seconds (doubles each retry).

    Returns:
        The result of the function call.

    Raises:
        RuntimeError: If all retries fail without raising an exception.
    """
    from src.ratelimit import polymarket_limiter

    last_exception: Exception | None = None
    for attempt in range(max_retries):
        try:
            polymarket_limiter.acquire()
            result: Any = func()
            return result
        except Exception as e:  # noqa: BLE001
            last_exception = e
            delay = base_delay * (2**attempt)
            logger.warning(
                "api_call_retry",
                attempt=attempt + 1,
                max_retries=max_retries,
                delay=delay,
                error=str(e),
            )
            time.sleep(delay)
    if last_exception is not None:
        raise last_exception
    msg = "Retry logic failed without exception"
    raise RuntimeError(msg)


class PolymarketClient:
    """Read-only Polymarket client for weather market scanning.

    Uses the Gamma API for server-side weather market search
    and py-clob-client for CLOB-specific operations.
    """

    def __init__(self, host: str = POLYMARKET_HOST) -> None:
        """Initialize the Polymarket client.

        Args:
            host: Polymarket CLOB API host URL.
        """
        self._client: Any = ClobClient(host)
        self._http = httpx.Client(
            base_url=GAMMA_API_URL,
            timeout=30.0,
        )
        self._clob_http = httpx.Client(
            base_url=host,
            timeout=30.0,
        )
        logger.info("polymarket_client_initialized", host=host)

    def close(self) -> None:
        """Close HTTP clients."""
        self._http.close()
        self._clob_http.close()

    def get_weather_markets(self) -> list[WeatherMarket]:
        """Fetch weather markets using the Gamma API events endpoint.

        Queries the Gamma events API by tag_slug for weather-related categories
        and extracts nested markets from each event.

        Returns:
            List of parsed WeatherMarket objects.
        """
        tag_slugs = ["temperature", "precipitation", "snowfall", "weather"]
        seen_ids: set[str] = set()
        weather_markets: list[WeatherMarket] = []

        for tag_slug in tag_slugs:
            markets = self._fetch_events_markets(tag_slug)
            for market_data in markets:
                condition_id = str(
                    market_data.get("conditionId", market_data.get("condition_id", ""))
                )
                if not condition_id or condition_id in seen_ids:
                    continue
                seen_ids.add(condition_id)
                parsed = self._try_parse_weather_market(market_data)
                if parsed is not None:
                    weather_markets.append(parsed)

        logger.info("weather_markets_found", count=len(weather_markets))
        return weather_markets

    def get_resolved_weather_markets(self, lookback_days: int = 7) -> list[WeatherMarket]:
        """Fetch recently resolved (closed) weather markets from Gamma API.

        Args:
            lookback_days: How many days back to search for resolved markets.

        Returns:
            List of parsed WeatherMarket objects that have closed.
        """
        tag_slugs = ["temperature", "precipitation", "snowfall", "weather"]
        seen_ids: set[str] = set()
        weather_markets: list[WeatherMarket] = []

        for tag_slug in tag_slugs:
            markets = self._fetch_events_markets(tag_slug, closed=True)
            for market_data in markets:
                condition_id = str(
                    market_data.get("conditionId", market_data.get("condition_id", ""))
                )
                if not condition_id or condition_id in seen_ids:
                    continue
                seen_ids.add(condition_id)
                parsed = self._try_parse_weather_market(market_data)
                if parsed is not None:
                    # Filter by lookback window
                    days_ago = (date.today() - parsed.event_date).days
                    if 0 < days_ago <= lookback_days:
                        weather_markets.append(parsed)

        logger.info("resolved_weather_markets_found", count=len(weather_markets))
        return weather_markets

    def get_price_history(
        self, token_id: str, start_ts: int, end_ts: int
    ) -> list[tuple[int, Decimal]]:
        """Fetch historical price data for a market token from the CLOB API.

        Args:
            token_id: The outcome token ID.
            start_ts: Start timestamp (Unix seconds).
            end_ts: End timestamp (Unix seconds).

        Returns:
            List of (timestamp, price) tuples sorted by time.
        """
        logger.info(
            "fetching_price_history",
            token_id=token_id[:20],
            start_ts=start_ts,
            end_ts=end_ts,
        )

        try:
            response = self._clob_http.get(
                "/prices-history",
                params={
                    "market": token_id,
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "fidelity": 60,
                },
            )
            response.raise_for_status()
            data: Any = response.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            logger.warning("price_history_error", token_id=token_id[:20], error=str(e))
            return []

        prices: list[tuple[int, Decimal]] = []
        if isinstance(data, dict):
            history: Any = data.get("history", [])
            if isinstance(history, list):
                for entry in history:
                    if isinstance(entry, dict):
                        ts = entry.get("t")
                        price = entry.get("p")
                        if ts is not None and price is not None:
                            try:
                                prices.append((int(ts), Decimal(str(price))))
                            except (ValueError, InvalidOperation):
                                continue

        logger.info("price_history_fetched", token_id=token_id[:20], points=len(prices))
        return sorted(prices, key=lambda x: x[0])

    def _fetch_events_markets(self, tag_slug: str, closed: bool = False) -> list[dict[str, Any]]:
        """Fetch markets nested inside events for a given tag slug.

        Args:
            tag_slug: The Gamma API tag slug (e.g., "temperature", "precipitation").
            closed: If True, fetch closed/resolved markets instead of active ones.

        Returns:
            Flat list of market dicts extracted from events.
        """
        all_markets: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        max_pages = 5

        for page in range(max_pages):
            logger.info("gamma_events_fetch", tag_slug=tag_slug, offset=offset, page=page + 1)
            try:
                response = self._http.get(
                    "/events",
                    params={
                        "tag_slug": tag_slug,
                        "active": "false" if closed else "true",
                        "closed": "true" if closed else "false",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                response.raise_for_status()
                events: Any = response.json()
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                logger.warning("gamma_events_error", tag_slug=tag_slug, error=str(e))
                break

            if not isinstance(events, list) or len(events) == 0:
                break

            for event in events:
                if not isinstance(event, dict):
                    continue
                markets: Any = event.get("markets", [])
                if isinstance(markets, list):
                    for m in markets:
                        if isinstance(m, dict):
                            all_markets.append(m)

            if len(events) < limit:
                break
            offset += limit

        logger.info(
            "gamma_events_complete", tag_slug=tag_slug, markets_found=len(all_markets)
        )
        return all_markets

    def get_market_price(self, token_id: str) -> Decimal:
        """Get the midpoint price for a market token.

        Args:
            token_id: The outcome token ID.

        Returns:
            Midpoint price as Decimal.
        """
        logger.info("fetching_market_price", token_id=token_id[:20])
        result: Any = _retry_with_backoff(lambda: self._client.get_midpoint(token_id))
        try:
            return Decimal(str(result))
        except (InvalidOperation, TypeError):
            logger.error("invalid_price_response", result=result)
            return Decimal("0")

    def _try_parse_weather_market(self, data: dict[str, Any]) -> WeatherMarket | None:
        """Attempt to parse a market dict into a WeatherMarket.

        Args:
            data: Raw market data from the Polymarket API.

        Returns:
            WeatherMarket if it's a weather contract, None otherwise.
        """
        question: str = str(data.get("question", ""))
        q_lower = question.lower()

        if not any(kw in q_lower for kw in WEATHER_KEYWORDS):
            return None

        # Gamma API uses camelCase, CLOB uses snake_case
        market_id: str = str(
            data.get("conditionId", data.get("condition_id", data.get("market_id", "")))
        )
        if not market_id:
            return None

        # Gamma uses "outcomePrices" (JSON string), CLOB uses "outcome_prices" (list)
        outcome_prices_raw: Any = data.get("outcomePrices", data.get("outcome_prices", []))
        outcome_prices: Any = outcome_prices_raw
        if isinstance(outcome_prices_raw, str):
            try:
                outcome_prices = json.loads(outcome_prices_raw)
            except (json.JSONDecodeError, TypeError):
                outcome_prices = []
        yes_price: Decimal
        no_price: Decimal
        if isinstance(outcome_prices, list) and len(outcome_prices) >= 2:
            try:
                yes_price = Decimal(str(outcome_prices[0]))
                no_price = Decimal(str(outcome_prices[1]))
            except (InvalidOperation, IndexError):
                return None
        else:
            return None

        # Parse location, date, metric, threshold from question
        parsed = _parse_weather_question(question)
        if parsed is None:
            return None

        location, lat, lon, event_date, metric, threshold, comparison = parsed

        try:
            volume = Decimal(str(data.get("volume", data.get("volumeNum", "0"))))
        except InvalidOperation:
            volume = Decimal("0")

        # Gamma uses "endDate", CLOB uses "close_date" / "end_date_iso"
        close_date_str: str = str(
            data.get("endDate", data.get("close_date", data.get("end_date_iso", "")))
        )
        close_date = _parse_datetime(close_date_str)
        if close_date is None:
            return None

        # Parse market creation time for freshness scoring
        created_at_str: str = str(
            data.get("createdAt", data.get("created_at", ""))
        )
        created_at = _parse_datetime(created_at_str)

        # Gamma uses "clobTokenIds" (JSON string), CLOB uses "tokens" array
        token_id = ""
        clob_token_ids: Any = data.get("clobTokenIds")
        if isinstance(clob_token_ids, str):
            try:
                ids = json.loads(clob_token_ids)
                if isinstance(ids, list) and len(ids) > 0:
                    token_id = str(ids[0])
            except (json.JSONDecodeError, TypeError):
                pass
        if not token_id:
            tokens: Any = data.get("tokens", [])
            if isinstance(tokens, list) and len(tokens) > 0:
                first_token: Any = tokens[0]
                if isinstance(first_token, dict):
                    token_id = str(first_token.get("token_id", ""))

        return WeatherMarket(
            market_id=market_id,
            question=question,
            location=location,
            lat=lat,
            lon=lon,
            event_date=event_date,
            metric=metric,
            threshold=threshold,
            comparison=comparison,
            yes_price=yes_price,
            no_price=no_price,
            volume=volume,
            close_date=close_date,
            token_id=token_id,
            created_at=created_at,
        )


def _parse_weather_question(
    question: str,
) -> tuple[str, float, float, date, MetricType, float, ComparisonType] | None:
    """Parse a weather market question to extract structured data.

    Args:
        question: The market question text.

    Returns:
        Tuple of (location, lat, lon, event_date, metric, threshold, comparison)
        or None if parsing fails.
    """
    q_lower = question.lower()

    # Find location — sort by length (longest first) so "las vegas" matches before "la"
    location = ""
    lat = 0.0
    lon = 0.0
    for city, coords in sorted(CITY_COORDS.items(), key=lambda x: len(x[0]), reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", q_lower):
            location = city.title()
            lat, lon = coords
            break

    if not location:
        return None

    # Determine metric — check precip/snow FIRST to avoid "below" matching "low"
    metric: MetricType = "temperature_high"
    if "precip" in q_lower or "rain" in q_lower:
        metric = "precipitation"
    elif "snow" in q_lower:
        metric = "snowfall"
    elif "low temp" in q_lower or "temperature low" in q_lower or re.search(r"\blow\b", q_lower):
        metric = "temperature_low"
    elif "high temp" in q_lower or "high" in q_lower:
        metric = "temperature_high"

    # Extract threshold number — prefer numbers with unit markers to avoid matching dates
    threshold = 0.0
    # Try specific patterns first: "75°F", "0.1 inches", "32 degrees"
    threshold_match = re.search(
        r"(\d+\.?\d*)\s*(?:°[fFcC]|degrees|inches|in\b)", question
    )
    if not threshold_match:
        # Fallback: number after "above/below/exceed/over/under/reach"
        threshold_match = re.search(
            r"(?:above|below|exceed|over|under|reach|than)\s+(\d+\.?\d*)", q_lower
        )
    if threshold_match:
        threshold = float(threshold_match.group(1))

    # Determine comparison
    comparison: ComparisonType = "above"
    if "below" in q_lower or "under" in q_lower or "less than" in q_lower:
        comparison = "below"
    elif "between" in q_lower:
        comparison = "between"

    # Extract date
    event_date: date | None = None
    today = date.today()
    date_pattern = r"(?:on\s+)?(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?"
    for match in re.finditer(date_pattern, question, re.IGNORECASE):
        month_str = match.group(1).lower()
        if month_str in MONTHS:
            day = int(match.group(2))
            if match.group(3):
                year = int(match.group(3))
            else:
                # Infer year: if the date would be >6 months in the past, use next year
                year = today.year
                try:
                    candidate = date(year, MONTHS[month_str], day)
                except ValueError:
                    continue
                if (today - candidate).days > 180:
                    year += 1
            try:
                event_date = date(year, MONTHS[month_str], day)
            except ValueError:
                continue
            break

    if event_date is None:
        return None

    return location, lat, lon, event_date, metric, threshold, comparison


def _parse_datetime(date_str: str) -> datetime | None:
    """Parse an ISO datetime string.

    Args:
        date_str: ISO format datetime string.

    Returns:
        Parsed datetime or None.
    """
    if not date_str:
        return None
    for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"]:
        try:
            parsed = datetime.strptime(date_str, fmt)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
            return parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
