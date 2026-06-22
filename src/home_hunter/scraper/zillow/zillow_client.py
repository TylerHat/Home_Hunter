"""HTTP client for Zillow using curl_cffi browser (TLS/JA3) impersonation.

This is the single most valuable free defense against Zillow's PerimeterX +
Cloudflare stack: requests carry a real Chrome TLS fingerprint instead of a
Python one. We add realistic headers, randomized delays, and exponential
backoff on blocks (403/429), and raise ``BlockedError`` when a target stays
blocked so callers can skip-and-log rather than hammer.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from ...config import RateLimit

logger = logging.getLogger(__name__)

# curl_cffi is optional at import time so offline parser tests don't require it.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover - exercised only without the dep installed
    cffi_requests = None  # type: ignore

# Sent on every request. The User-Agent / sec-ch-ua client hints are supplied by
# curl_cffi's browser impersonation; we only add what it doesn't.
BASE_HEADERS = {
    "accept-language": "en-US,en;q=0.9",
    "accept-encoding": "gzip, deflate, br, zstd",
}

# Headers a real browser sends for a top-level page navigation (the search HTML).
# Crucially: accept text/html, navigate mode, no `origin`, no XHR markers.
DOCUMENT_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "priority": "u=0, i",
}

# Headers for the in-page XHR to the GetSearchPageState JSON endpoint. These must
# follow a document load on the same session so the cookies/referer line up.
API_HEADERS = {
    "accept": "application/json,text/javascript,*/*;q=0.01",
    "referer": "https://www.zillow.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "x-requested-with": "XMLHttpRequest",
    "priority": "u=1, i",
}


class BlockedError(RuntimeError):
    """Raised when Zillow blocks the request after all retries."""


class ZillowClient:
    """Thin wrapper around a curl_cffi session with retry/backoff + pacing."""

    def __init__(self, rate_limit: RateLimit | None = None) -> None:
        if cffi_requests is None:
            raise RuntimeError(
                "curl_cffi is not installed. Run `pip install -r requirements.txt`."
            )
        self.rate = rate_limit or RateLimit()
        # `impersonate` is a legacy RateLimit field; default to chrome if absent.
        self.session = cffi_requests.Session(
            impersonate=getattr(self.rate, "impersonate", "chrome")
        )
        self.session.headers.update(BASE_HEADERS)

    def _sleep_between_requests(self) -> None:
        delay = random.uniform(self.rate.min_delay_seconds, self.rate.max_delay_seconds)
        logger.debug("sleeping %.1fs before next request", delay)
        time.sleep(delay)

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        pace: bool = True,
    ) -> Any:
        """GET with retry/backoff on 403/429. Returns the curl_cffi Response."""
        last_status: int | None = None
        for attempt in range(1, self.rate.max_retries + 1):
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            last_status = resp.status_code
            if resp.status_code == 200:
                if pace:
                    self._sleep_between_requests()
                return resp
            if resp.status_code in (403, 429, 503):
                backoff = self.rate.backoff_base_seconds * (2 ** (attempt - 1))
                backoff += random.uniform(0, self.rate.backoff_base_seconds)
                logger.warning(
                    "blocked (HTTP %s) on %s — attempt %d/%d, backing off %.1fs",
                    resp.status_code, url, attempt, self.rate.max_retries, backoff,
                )
                time.sleep(backoff)
                continue
            resp.raise_for_status()
        raise BlockedError(
            f"Zillow blocked {url} after {self.rate.max_retries} attempts "
            f"(last status {last_status})."
        )

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        headers = dict(API_HEADERS)
        if referer:
            headers["referer"] = referer
        return self.get(url, params=params, headers=headers).json()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return self.get(url, params=params, headers=DOCUMENT_HEADERS).text

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "ZillowClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
