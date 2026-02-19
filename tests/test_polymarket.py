"""Tests for _parse_weather_question in the polymarket module."""

from __future__ import annotations

from datetime import date

import pytest

from src.polymarket import _parse_weather_question

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metric(result: tuple | None) -> str | None:
    return result[4] if result else None

def _location(result: tuple | None) -> str | None:
    return result[0] if result else None

def _threshold(result: tuple | None) -> float | None:
    return result[5] if result else None

def _comparison(result: tuple | None) -> str | None:
    return result[6] if result else None

def _event_date(result: tuple | None) -> date | None:
    return result[3] if result else None


# ---------------------------------------------------------------------------
# Metric detection
# ---------------------------------------------------------------------------

class TestMetricDetection:
    """Tests for weather metric classification."""

    def test_precipitation_keyword(self) -> None:
        q = "Will precipitation exceed 0.5 inches in NYC on March 5?"
        assert _metric(_parse_weather_question(q)) == "precipitation"

    def test_rain_keyword(self) -> None:
        q = "Will there be rain above 0.1 inches in Chicago on April 10?"
        assert _metric(_parse_weather_question(q)) == "precipitation"

    def test_snow_keyword(self) -> None:
        q = "Will snow exceed 3 inches in Boston on January 15?"
        assert _metric(_parse_weather_question(q)) == "snowfall"

    def test_low_temp_phrase(self) -> None:
        q = "Will the low temp be below 32\u00b0F in NYC on March 5?"
        assert _metric(_parse_weather_question(q)) == "temperature_low"

    def test_standalone_low_word(self) -> None:
        q = "Will the low be under 25\u00b0F in Denver on March 1?"
        assert _metric(_parse_weather_question(q)) == "temperature_low"

    def test_high_temp_phrase(self) -> None:
        q = "Will the high temp exceed 90\u00b0F in Phoenix on July 4?"
        assert _metric(_parse_weather_question(q)) == "temperature_high"

    def test_default_metric_is_temperature_high(self) -> None:
        q = "Will NYC reach 80\u00b0F on August 1?"
        assert _metric(_parse_weather_question(q)) == "temperature_high"


# ---------------------------------------------------------------------------
# CRITICAL: precip/snow checked BEFORE "low"
# ---------------------------------------------------------------------------

class TestPrecipBeforeLow:
    """Precip keywords checked before 'low' so 'below' doesn't trigger temp_low."""

    def test_precipitation_below_does_not_match_low(self) -> None:
        q = "Will precipitation be below 0.1 inches in NYC on March 5?"
        result = _parse_weather_question(q)
        assert _metric(result) == "precipitation"
        assert _comparison(result) == "below"

    def test_rain_below_does_not_match_low(self) -> None:
        q = "Will rain be below 0.5 inches in Miami on April 20?"
        assert _metric(_parse_weather_question(q)) == "precipitation"

    def test_snowfall_below_does_not_match_low(self) -> None:
        q = "Will snowfall be below 2 inches in Chicago on December 15?"
        assert _metric(_parse_weather_question(q)) == "snowfall"


# ---------------------------------------------------------------------------
# City matching with word boundaries
# ---------------------------------------------------------------------------

class TestCityMatching:
    """City matching uses word boundaries and longest-first sort."""

    def test_dallas_does_not_match_la(self) -> None:
        q = "Will the high exceed 95\u00b0F in Dallas on July 10?"
        assert _location(_parse_weather_question(q)) == "Dallas"

    def test_las_vegas_matches_before_la(self) -> None:
        q = "Will the high exceed 110\u00b0F in Las Vegas on July 4?"
        assert _location(_parse_weather_question(q)) == "Las Vegas"

    def test_los_angeles_matches_before_la(self) -> None:
        q = "Will the high exceed 95\u00b0F in Los Angeles on August 5?"
        assert _location(_parse_weather_question(q)) == "Los Angeles"

    def test_new_york_full_name(self) -> None:
        q = "Will the high exceed 85\u00b0F in New York on July 20?"
        assert _location(_parse_weather_question(q)) == "New York"

    def test_san_francisco_full_name(self) -> None:
        q = "Will precipitation exceed 0.5 inches in San Francisco on November 10?"
        assert _location(_parse_weather_question(q)) == "San Francisco"

    def test_no_matching_city_returns_none(self) -> None:
        q = "Will the high exceed 80\u00b0F in Topeka on March 5?"
        assert _parse_weather_question(q) is None


# ---------------------------------------------------------------------------
# Threshold extraction
# ---------------------------------------------------------------------------

class TestThresholdExtraction:
    """Tests for numeric threshold parsing."""

    def test_degrees_fahrenheit_marker(self) -> None:
        q = "Will the high exceed 75\u00b0F in NYC on March 5?"
        assert _threshold(_parse_weather_question(q)) == 75.0

    def test_inches_marker(self) -> None:
        q = "Will precipitation exceed 0.1 inches in Chicago on April 10?"
        assert _threshold(_parse_weather_question(q)) == pytest.approx(0.1)

    def test_degrees_marker(self) -> None:
        q = "Will the low drop below 32 degrees in Boston on January 15?"
        assert _threshold(_parse_weather_question(q)) == 32.0

    def test_fallback_after_above(self) -> None:
        q = "Will the temperature be above 100 in Phoenix on July 4?"
        assert _threshold(_parse_weather_question(q)) == 100.0

    def test_fallback_after_below(self) -> None:
        q = "Will the temperature drop below 20 in Denver on January 3?"
        assert _threshold(_parse_weather_question(q)) == 20.0

    def test_decimal_threshold(self) -> None:
        q = "Will precipitation exceed 0.5 inches in Seattle on November 5?"
        assert _threshold(_parse_weather_question(q)) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

class TestDateParsing:
    """Tests for date extraction from question text."""

    def test_month_day_with_year(self) -> None:
        q = "Will the high exceed 75\u00b0F in NYC on March 5, 2027?"
        assert _event_date(_parse_weather_question(q)) == date(2027, 3, 5)

    def test_abbreviated_month(self) -> None:
        q = "Will the high exceed 60\u00b0F in NYC on Feb 14?"
        ed = _event_date(_parse_weather_question(q))
        assert ed is not None
        assert ed.month == 2
        assert ed.day == 14

    def test_ordinal_suffix_th(self) -> None:
        q = "Will the high exceed 75\u00b0F in NYC on March 5th?"
        ed = _event_date(_parse_weather_question(q))
        assert ed is not None
        assert ed.month == 3
        assert ed.day == 5

    def test_ordinal_suffix_st(self) -> None:
        q = "Will the high exceed 30\u00b0F in Chicago on January 1st?"
        ed = _event_date(_parse_weather_question(q))
        assert ed is not None
        assert ed.day == 1

    def test_no_date_returns_none(self) -> None:
        q = "Will the high exceed 80\u00b0F in NYC?"
        assert _parse_weather_question(q) is None


# ---------------------------------------------------------------------------
# Comparison detection
# ---------------------------------------------------------------------------

class TestComparisonDetection:
    """Tests for above/below/between classification."""

    def test_default_is_above(self) -> None:
        q = "Will the temperature reach 80\u00b0F in NYC on March 5?"
        assert _comparison(_parse_weather_question(q)) == "above"

    def test_below_keyword(self) -> None:
        q = "Will the high be below 60\u00b0F in Seattle on November 10?"
        assert _comparison(_parse_weather_question(q)) == "below"

    def test_under_keyword(self) -> None:
        q = "Will the high be under 50\u00b0F in Denver on December 5?"
        assert _comparison(_parse_weather_question(q)) == "below"

    def test_less_than_keyword(self) -> None:
        q = "Will the high be less than 40\u00b0F in Chicago on January 20?"
        assert _comparison(_parse_weather_question(q)) == "below"

    def test_between_keyword(self) -> None:
        q = "Will the temperature be between 60 and 80\u00b0F in NYC on March 5?"
        assert _comparison(_parse_weather_question(q)) == "between"


# ---------------------------------------------------------------------------
# Returns None edge cases
# ---------------------------------------------------------------------------

class TestReturnsNone:
    """Cases where _parse_weather_question returns None."""

    def test_no_matching_city(self) -> None:
        q = "Will the high exceed 80\u00b0F in Topeka on March 5?"
        assert _parse_weather_question(q) is None

    def test_no_parseable_date(self) -> None:
        assert _parse_weather_question("Will the high exceed 80\u00b0F in NYC?") is None

    def test_empty_string(self) -> None:
        assert _parse_weather_question("") is None

    def test_nonsense_string(self) -> None:
        assert _parse_weather_question("asdfghjkl 12345") is None


# ---------------------------------------------------------------------------
# Full parse integration
# ---------------------------------------------------------------------------

class TestFullParse:
    """End-to-end tests verifying all returned fields."""

    def test_standard_temperature_question(self) -> None:
        q = "Will the high temp exceed 75\u00b0F in New York on March 5, 2027?"
        result = _parse_weather_question(q)
        assert result is not None
        location, lat, lon, event_date, metric, threshold, comparison = result
        assert location == "New York"
        assert abs(lat - 40.7128) < 0.01
        assert event_date == date(2027, 3, 5)
        assert metric == "temperature_high"
        assert threshold == 75.0
        assert comparison == "above"

    def test_precipitation_below_question(self) -> None:
        q = "Will precipitation be below 0.1 inches in Chicago on April 10, 2027?"
        result = _parse_weather_question(q)
        assert result is not None
        location, lat, lon, event_date, metric, threshold, comparison = result
        assert location == "Chicago"
        assert metric == "precipitation"
        assert threshold == pytest.approx(0.1)
        assert comparison == "below"
