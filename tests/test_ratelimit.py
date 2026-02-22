"""Tests for the token bucket rate limiter."""

from __future__ import annotations

import time

from src.ratelimit import TokenBucket


class TestTokenBucket:
    """Tests for TokenBucket rate limiter."""

    def test_acquire_succeeds_within_burst(self) -> None:
        bucket = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            assert bucket.acquire(timeout=0.1)

    def test_acquire_blocks_when_empty(self) -> None:
        bucket = TokenBucket(rate=100.0, burst=1)
        assert bucket.acquire(timeout=0.1)
        # Second acquire should succeed quickly since rate is high
        assert bucket.acquire(timeout=0.1)

    def test_acquire_times_out(self) -> None:
        bucket = TokenBucket(rate=0.1, burst=1)
        assert bucket.acquire(timeout=0.1)
        # Very slow refill, should time out
        assert not bucket.acquire(timeout=0.05)

    def test_tokens_refill_over_time(self) -> None:
        bucket = TokenBucket(rate=100.0, burst=2)
        # Drain the bucket
        assert bucket.acquire(timeout=0.01)
        assert bucket.acquire(timeout=0.01)
        # Wait for refill
        time.sleep(0.05)
        assert bucket.acquire(timeout=0.01)

    def test_burst_caps_tokens(self) -> None:
        bucket = TokenBucket(rate=1000.0, burst=3)
        time.sleep(0.1)  # More than enough time to fill up
        # Should only be able to get burst amount immediately
        for _ in range(3):
            assert bucket.acquire(timeout=0.01)
