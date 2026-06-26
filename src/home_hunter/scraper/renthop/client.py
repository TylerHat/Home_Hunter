"""HTTP client for RentHop using curl_cffi Chrome (TLS/JA3) impersonation.

RentHop sits behind Cloudflare. A plain Python TLS fingerprint (``httpx``) gets
a 403 "Just a moment" interstitial, but a real Chrome TLS handshake clears
Cloudflare's passive check — so curl_cffi's browser impersonation is enough and
**no headless browser is needed**, keeping the scrape fast and CI-friendly like
Craigslist.

The client exposes the **same surface** as ``CraigslistClient``
(``get_text``/``close`` + ``BlockedError``) so ``search.py`` and the pipeline
treat RentHop like any other source.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import ssl
import tempfile
import time

from ...config import RateLimit

logger = logging.getLogger(__name__)

# curl_cffi is optional at import time so the offline parser tests don't need it.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
except Exception:  # pragma: no cover - exercised only without the dep installed
    cffi_requests = None  # type: ignore

# curl_cffi ships its own libcurl + CA bundle and does NOT honor the ssl-module
# patch ``truststore`` applies for httpx. So behind a corporate TLS-inspection
# proxy — whose root CA lives only in the OS store — curl's default verification
# fails with CERTIFICATE_VERIFY_FAILED. Mirror what truststore does: export the
# OS (Windows) cert store to a PEM bundle and verify against it. No-op on CI /
# Linux, where ``ssl.enum_certificates`` is absent and certifi already works.
_CA_BUNDLE_CACHE: str | None = None


def _os_ca_bundle() -> str | None:
    """A CA-bundle PEM built from the Windows trust store, or ``None`` elsewhere.

    Includes any corporate proxy root CA, so curl_cffi keeps verifying TLS
    instead of disabling it. Built once and cached for the process lifetime.
    """
    global _CA_BUNDLE_CACHE
    if _CA_BUNDLE_CACHE is not None:
        return _CA_BUNDLE_CACHE or None

    pem: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            certs = ssl.enum_certificates(store)  # Windows-only API
        except (AttributeError, OSError):  # not Windows / store unavailable
            continue
        for cert, encoding, _trust in certs:
            if encoding == "x509_asn":
                b64 = base64.b64encode(cert).decode("ascii")
                body = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
                pem.append(
                    f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n"
                )

    if not pem:
        _CA_BUNDLE_CACHE = ""  # sentinel: looked, found nothing — don't look again
        return None
    fd, path = tempfile.mkstemp(suffix="_ca.pem", prefix="home_hunter_")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write("".join(pem))
    _CA_BUNDLE_CACHE = path
    return path


class BlockedError(RuntimeError):
    """Raised when RentHop/Cloudflare blocks the request after retries."""


# Markers unique to Cloudflare's interstitial (never present on a results page).
_BLOCK_MARKERS = ("just a moment", "attention required", "cf-chl-", "challenge-platform")


class RentHopClient:
    """Paced curl_cffi GET with retry/backoff. Usable as a context manager."""

    def __init__(self, rate_limit: RateLimit) -> None:
        if cffi_requests is None:
            raise RuntimeError(
                "curl_cffi is not installed. Run `pip install -r requirements.txt`."
            )
        self._rl = rate_limit
        # Secure by default. Prefer the OS trust store (covers a corporate proxy);
        # HOME_HUNTER_INSECURE_TLS=1 is a documented last-resort escape hatch.
        verify: object = True
        if os.getenv("HOME_HUNTER_INSECURE_TLS") == "1":
            verify = False
        else:
            bundle = _os_ca_bundle()
            if bundle:
                verify = bundle
        self._session = cffi_requests.Session(impersonate="chrome", verify=verify)
        self._session.headers.update(
            {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._first_request = True

    def __enter__(self) -> "RentHopClient":
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

    @staticmethod
    def _looks_blocked(status: int, body: str) -> bool:
        if status in (403, 429, 503):
            return True
        head = body[:2000].lower()
        return any(marker in head for marker in _BLOCK_MARKERS)

    def get_text(self, url: str, params: dict | None = None) -> str:
        """GET a URL, retrying on Cloudflare blocks / transient errors with backoff."""
        last_exc: Exception | None = None
        for attempt in range(1, self._rl.max_retries + 1):
            self._sleep_between()
            try:
                resp = self._session.get(url, params=params, timeout=30)
            except Exception as exc:  # curl/network error
                last_exc = exc
                logger.warning("GET %s attempt %d errored: %s", url, attempt, exc)
            else:
                body = resp.text or ""
                if resp.status_code == 200 and not self._looks_blocked(resp.status_code, body):
                    return body
                last_exc = BlockedError(f"HTTP {resp.status_code} for {url}")
                logger.warning(
                    "GET %s attempt %d -> blocked (HTTP %d)", url, attempt, resp.status_code
                )
            if attempt < self._rl.max_retries:
                time.sleep(self._rl.backoff_base_seconds * (2 ** (attempt - 1)))

        raise BlockedError(str(last_exc) if last_exc else f"failed to GET {url}")

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover - best effort
            pass
