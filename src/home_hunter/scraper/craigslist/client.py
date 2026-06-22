"""HTTP client for Craigslist: a browser User-Agent, paced requests, retries.

Craigslist has light bot protection (no JS challenge), so a plain HTTP client
with a real User-Agent and polite pacing is enough — no headless browser, which
keeps the scrape fast, light, and reliable on free CI runners.
"""

from __future__ import annotations

import logging
import random
import time

import httpx

from ...config import RateLimit

logger = logging.getLogger(__name__)


class BlockedError(RuntimeError):
    """Raised when Craigslist returns a hard block (403/429) after retries."""


class CraigslistClient:
    """Paced HTTP GET with retry/backoff. Usable as a context manager."""

    def __init__(self, rate_limit: RateLimit) -> None:
        self._rl = rate_limit
        self._client = httpx.Client(
            headers={
                "User-Agent": rate_limit.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=30.0,
        )
        self._first_request = True

    def __enter__(self) -> "CraigslistClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _sleep_between(self) -> None:
        """Random delay between requests (skipped before the very first)."""
        if self._first_request:
            self._first_request = False
            return
        delay = random.uniform(self._rl.min_delay_seconds, self._rl.max_delay_seconds)
        time.sleep(delay)

    def get_text(self, url: str, params: dict | None = None) -> str:
        """GET a URL, retrying on transient blocks/errors with backoff."""
        last_exc: Exception | None = None
        for attempt in range(1, self._rl.max_retries + 1):
            self._sleep_between()
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:  # network blip
                last_exc = exc
                logger.warning("GET %s attempt %d errored: %s", url, attempt, exc)
            else:
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    logger.warning(
                        "GET %s attempt %d -> HTTP %d", url, attempt, resp.status_code
                    )
                    last_exc = BlockedError(f"HTTP {resp.status_code} for {url}")
                else:
                    resp.raise_for_status()
                    return resp.text
            # Exponential backoff before the next attempt.
            if attempt < self._rl.max_retries:
                time.sleep(self._rl.backoff_base_seconds * (2 ** (attempt - 1)))

        raise BlockedError(str(last_exc) if last_exc else f"failed to GET {url}")

    def close(self) -> None:
        self._client.close()
