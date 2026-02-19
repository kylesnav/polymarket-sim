"""Tests for the NOAA weather API client."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.noaa import NOAAClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client() -> NOAAClient:
    """Create a NOAAClient with a mocked httpx client."""
    c = NOAAClient.__new__(NOAAClient)
    c._http = MagicMock(spec=httpx.Client)
    c._grid_cache = {}
    c._station_cache = {}
    return c


def _make_response(json_data: Any, status_code: int = 200) -> MagicMock:
    """Build a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = json_data
    resp.status_code = status_code
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# Grid info
# ---------------------------------------------------------------------------

class TestGetGridInfo:
    """Tests for _get_grid_info method."""

    def test_returns_office_and_grid(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({
            "properties": {"gridId": "OKX", "gridX": 33, "gridY": 37},
        })
        result = client._get_grid_info(40.7128, -74.0060)
        assert result == ("OKX", 33, 37)

    def test_caches_result(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({
            "properties": {"gridId": "OKX", "gridX": 33, "gridY": 37},
        })
        client._get_grid_info(40.7128, -74.0060)
        client._get_grid_info(40.7128, -74.0060)
        assert client._http.get.call_count == 1

    def test_returns_none_on_error(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({}, 500)
        result = client._get_grid_info(40.7128, -74.0060)
        assert result is None

    def test_returns_none_on_missing_office(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({
            "properties": {"gridId": "", "gridX": 33, "gridY": 37},
        })
        result = client._get_grid_info(40.7128, -74.0060)
        assert result is None


# ---------------------------------------------------------------------------
# Forecast parsing
# ---------------------------------------------------------------------------

SAMPLE_FORECAST_DATA: dict[str, Any] = {
    "properties": {
        "periods": [
            {
                "startTime": "2027-03-05T06:00:00-05:00",
                "isDaytime": True,
                "temperature": 75,
                "probabilityOfPrecipitation": {"value": 20},
                "detailedForecast": "Sunny with a high of 75.",
            },
            {
                "startTime": "2027-03-05T18:00:00-05:00",
                "isDaytime": False,
                "temperature": 55,
                "probabilityOfPrecipitation": {"value": 10},
                "detailedForecast": "Clear with a low of 55.",
            },
            {
                "startTime": "2027-03-06T06:00:00-05:00",
                "isDaytime": True,
                "temperature": 78,
                "probabilityOfPrecipitation": {"value": 30},
                "detailedForecast": "Partly cloudy.",
            },
        ]
    }
}


class TestParseForecast:
    """Tests for _parse_forecast method."""

    def test_extracts_day_and_night_temps(self, client: NOAAClient) -> None:
        result = client._parse_forecast(
            SAMPLE_FORECAST_DATA, 40.71, -74.01, date(2027, 3, 5),
        )
        assert result is not None
        assert result.temperature_high == 75.0
        assert result.temperature_low == 55.0

    def test_extracts_precip_probability(self, client: NOAAClient) -> None:
        result = client._parse_forecast(
            SAMPLE_FORECAST_DATA, 40.71, -74.01, date(2027, 3, 5),
        )
        assert result is not None
        # 20% day → 0.20, but the last matching period overwrites
        assert result.precip_probability is not None
        assert 0.0 <= result.precip_probability <= 1.0

    def test_builds_narrative_from_periods(self, client: NOAAClient) -> None:
        result = client._parse_forecast(
            SAMPLE_FORECAST_DATA, 40.71, -74.01, date(2027, 3, 5),
        )
        assert result is not None
        assert "Sunny" in result.forecast_narrative
        assert "Clear" in result.forecast_narrative

    def test_returns_none_for_wrong_date(self, client: NOAAClient) -> None:
        result = client._parse_forecast(
            SAMPLE_FORECAST_DATA, 40.71, -74.01, date(2027, 3, 10),
        )
        assert result is None

    def test_returns_none_for_empty_periods(self, client: NOAAClient) -> None:
        data: dict[str, Any] = {"properties": {"periods": []}}
        result = client._parse_forecast(data, 40.71, -74.01, date(2027, 3, 5))
        assert result is None

    def test_returns_none_for_missing_properties(self, client: NOAAClient) -> None:
        result = client._parse_forecast({}, 40.71, -74.01, date(2027, 3, 5))
        assert result is None


# ---------------------------------------------------------------------------
# Observation parsing
# ---------------------------------------------------------------------------

SAMPLE_OBSERVATIONS: dict[str, Any] = {
    "features": [
        {
            "properties": {
                "temperature": {"value": 24.0, "unitCode": "wmoUnit:degC"},
                "precipitationLastHour": {"value": 2.54},
            }
        },
        {
            "properties": {
                "temperature": {"value": 18.0, "unitCode": "wmoUnit:degC"},
                "precipitationLastHour": {"value": 0},
            }
        },
        {
            "properties": {
                "temperature": {"value": 21.5, "unitCode": "wmoUnit:degC"},
                "precipitationLastHour": {"value": None},
            }
        },
    ]
}


class TestParseObservations:
    """Tests for _parse_observations method."""

    def test_converts_celsius_to_fahrenheit(self, client: NOAAClient) -> None:
        result = client._parse_observations(
            SAMPLE_OBSERVATIONS, "KNYC", 40.71, -74.01, date(2027, 3, 5),
        )
        assert result is not None
        # 24°C = 75.2°F (high), 18°C = 64.4°F (low)
        assert result.temperature_high == pytest.approx(75.2, abs=0.1)
        assert result.temperature_low == pytest.approx(64.4, abs=0.1)

    def test_sums_precipitation_and_converts_to_inches(self, client: NOAAClient) -> None:
        result = client._parse_observations(
            SAMPLE_OBSERVATIONS, "KNYC", 40.71, -74.01, date(2027, 3, 5),
        )
        assert result is not None
        # 2.54mm = 0.10 inches
        assert result.precipitation == pytest.approx(0.10, abs=0.01)

    def test_returns_none_for_no_temperature_data(self, client: NOAAClient) -> None:
        data: dict[str, Any] = {
            "features": [
                {"properties": {"temperature": {"value": None, "unitCode": "wmoUnit:degC"}}}
            ]
        }
        result = client._parse_observations(data, "KNYC", 40.71, -74.01, date(2027, 3, 5))
        assert result is None

    def test_returns_none_for_empty_features(self, client: NOAAClient) -> None:
        result = client._parse_observations(
            {"features": []}, "KNYC", 40.71, -74.01, date(2027, 3, 5),
        )
        assert result is None

    def test_handles_fahrenheit_unit(self, client: NOAAClient) -> None:
        data: dict[str, Any] = {
            "features": [
                {
                    "properties": {
                        "temperature": {"value": 75.0, "unitCode": "wmoUnit:degF"},
                        "precipitationLastHour": {"value": 0},
                    }
                }
            ]
        }
        result = client._parse_observations(data, "KNYC", 40.71, -74.01, date(2027, 3, 5))
        assert result is not None
        assert result.temperature_high == 75.0

    def test_handles_unknown_unit_as_fahrenheit(self, client: NOAAClient) -> None:
        data: dict[str, Any] = {
            "features": [
                {
                    "properties": {
                        "temperature": {"value": 65.0, "unitCode": "wmoUnit:unknownUnit"},
                        "precipitationLastHour": {"value": 0},
                    }
                }
            ]
        }
        result = client._parse_observations(data, "KNYC", 40.71, -74.01, date(2027, 3, 5))
        assert result is not None
        assert result.temperature_high == 65.0


# ---------------------------------------------------------------------------
# Station lookup
# ---------------------------------------------------------------------------

class TestGetNearestStation:
    """Tests for _get_nearest_station method."""

    def test_returns_station_id(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({
            "features": [
                {"properties": {"stationIdentifier": "KNYC"}},
                {"properties": {"stationIdentifier": "KJFK"}},
            ]
        })
        result = client._get_nearest_station(40.71, -74.01)
        assert result == "KNYC"

    def test_caches_station_id(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({
            "features": [{"properties": {"stationIdentifier": "KNYC"}}]
        })
        client._get_nearest_station(40.71, -74.01)
        client._get_nearest_station(40.71, -74.01)
        assert client._http.get.call_count == 1

    def test_returns_none_for_no_stations(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({"features": []})
        assert client._get_nearest_station(40.71, -74.01) is None

    def test_returns_none_on_http_error(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({}, 500)
        assert client._get_nearest_station(40.71, -74.01) is None


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

class TestRequestWithRetry:
    """Tests for _request_with_retry method."""

    def test_succeeds_on_first_try(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({"status": "ok"})
        result = client._request_with_retry("/test")
        assert result == {"status": "ok"}
        assert client._http.get.call_count == 1

    @patch("src.noaa.time.sleep")
    def test_retries_on_failure(self, mock_sleep: MagicMock, client: NOAAClient) -> None:
        fail_resp = _make_response({}, 500)
        ok_resp = _make_response({"status": "ok"})
        client._http.get.side_effect = [fail_resp, ok_resp]
        result = client._request_with_retry("/test", max_retries=2, base_delay=0.01)
        assert result == {"status": "ok"}
        assert client._http.get.call_count == 2

    @patch("src.noaa.time.sleep")
    def test_returns_none_after_max_retries(
        self, mock_sleep: MagicMock, client: NOAAClient,
    ) -> None:
        fail_resp = _make_response({}, 500)
        client._http.get.return_value = fail_resp
        result = client._request_with_retry("/test", max_retries=2, base_delay=0.01)
        assert result is None
        assert client._http.get.call_count == 2


# ---------------------------------------------------------------------------
# Full get_forecast flow
# ---------------------------------------------------------------------------

class TestGetForecast:
    """Tests for the full get_forecast method."""

    def test_returns_forecast_on_success(self, client: NOAAClient) -> None:
        # Mock grid info
        grid_resp = _make_response({
            "properties": {"gridId": "OKX", "gridX": 33, "gridY": 37},
        })
        forecast_resp = _make_response(SAMPLE_FORECAST_DATA)
        client._http.get.side_effect = [grid_resp, forecast_resp]

        result = client.get_forecast(40.71, -74.01, date(2027, 3, 5))
        assert result is not None
        assert result.temperature_high == 75.0

    def test_returns_none_when_grid_fails(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({}, 500)
        result = client.get_forecast(40.71, -74.01, date(2027, 3, 5))
        assert result is None


# ---------------------------------------------------------------------------
# Full get_observations flow
# ---------------------------------------------------------------------------

class TestGetObservations:
    """Tests for the full get_observations method."""

    def test_returns_observation_on_success(self, client: NOAAClient) -> None:
        station_resp = _make_response({
            "features": [{"properties": {"stationIdentifier": "KNYC"}}]
        })
        obs_resp = _make_response(SAMPLE_OBSERVATIONS)
        client._http.get.side_effect = [station_resp, obs_resp]

        result = client.get_observations(40.71, -74.01, date(2027, 3, 5))
        assert result is not None
        assert result.station_id == "KNYC"
        assert result.observation_date == date(2027, 3, 5)

    def test_returns_none_when_station_fails(self, client: NOAAClient) -> None:
        client._http.get.return_value = _make_response({"features": []})
        result = client.get_observations(40.71, -74.01, date(2027, 3, 5))
        assert result is None
