"""Token bucket rate limiter for external API calls.

Prevents hitting NOAA and Polymarket rate limits by throttling
outbound requests with configurable per-second rates.
"""

from __future__ import annotations

import threading
import time

import structlog

logger = structlog.get_logger()


class TokenBucket:
    """Thread-safe token bucket rate limiter.

    Tokens refill at a fixed rate. Each call to acquire() consumes
    one token, blocking until a token is available.

    Args:
        rate: Tokens added per second.
        burst: Maximum tokens that can accumulate.
    """

    def __init__(self, rate: float, burst: int | None = None) -> None:
        self._rate = rate
        self._burst = burst or int(rate * 2)
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 30.0) -> bool:
        """Acquire a token, blocking until one is available.

        Args:
            timeout: Maximum seconds to wait for a token.

        Returns:
            True if a token was acquired, False if timed out.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            # Wait a small interval before retrying
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))

    def _refill(self) -> None:
        """Add tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now


# Pre-configured limiters for external APIs
noaa_limiter = TokenBucket(rate=10.0, burst=20)
polymarket_limiter = TokenBucket(rate=5.0, burst=10)
