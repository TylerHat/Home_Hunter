"""Playwright-based Zillow client.

A plain HTTP client (curl_cffi) can't pass Zillow's PerimeterX because the
anti-bot cookie is minted by JavaScript that only a real browser runs. This
client drives a stealth-patched Chromium so that JS executes, the challenge
cookie is set, and subsequent same-origin API fetches succeed.

It deliberately exposes the **same surface** as ``ZillowClient``
(``get_text`` / ``get_json`` / ``close`` + ``BlockedError``) so ``search.py``
and the rest of the pipeline work unchanged — the backend is a config switch.

Setup (on the machine that runs the scrape):
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any
from urllib.parse import urlencode

from ...config import RateLimit
from .zillow_client import BlockedError

logger = logging.getLogger(__name__)

# Optional at import time so offline tests / curl_cffi-only setups don't need it.
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - exercised only without the dep installed
    sync_playwright = None  # type: ignore

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Runs before any page script — hides the most common headless tells.
_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(p);
}
try {
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';            // UNMASKED_VENDOR_WEBGL
    if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return _getParam.call(this, p);
  };
} catch (e) {}
"""

# POSITIVE signals that the page actually contains search results. If any of
# these is present we accept the page even though Zillow ships its PerimeterX
# *sensor* script (which mentions px / perimeterx) on every page, results or not.
_DATA_MARKERS = ('"zpid"', "searchresults", "mapbounds", "__next_data__")

# Phrases unique to the PerimeterX "Press & Hold" interstitial (never on results).
_CAPTCHA_MARKERS = (
    "press & hold",
    "px-captcha",
    "access to this page has been denied",
    "verify you are a human",
    "before we continue",
)


class PlaywrightZillowClient:
    """Drives a stealth Chromium; mirrors ZillowClient's get_text/get_json."""

    def __init__(
        self,
        rate_limit: RateLimit | None = None,
        *,
        headless: bool = True,
        debug_dir: str | None = None,
    ) -> None:
        if sync_playwright is None:
            raise RuntimeError(
                "playwright is not installed. Run:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        self.rate = rate_limit or RateLimit()
        self._debug_dir = debug_dir
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self.context = self.browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1366, "height": 900},
        )
        self.context.add_init_script(_STEALTH_JS)
        self.page = self.context.new_page()
        self._on_zillow = False

    # --- internal helpers ---------------------------------------------------

    def _pace(self) -> None:
        time.sleep(random.uniform(self.rate.min_delay_seconds, self.rate.max_delay_seconds))

    def _settle(self) -> None:
        """Human-like dwell: small pause, mouse move, partial scroll."""
        time.sleep(random.uniform(1.5, 3.5))
        try:
            self.page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            self.page.evaluate("window.scrollBy(0, Math.floor(Math.random()*600)+200)")
        except Exception:  # pragma: no cover - best effort
            pass
        time.sleep(random.uniform(0.5, 1.5))

    @staticmethod
    def _classify(status: int | None, html: str) -> str:
        """Return 'ok' | 'captcha' | 'blocked' | 'empty' for a fetched page.

        Success is decided by the *presence of listing data*, not by the absence
        of PerimeterX strings — Zillow ships its PX sensor on every page.
        """
        lowered = html.lower()
        if any(m in lowered for m in _DATA_MARKERS):
            return "ok"  # real results, even if PX sensor strings are present
        if status in (403, 429, 503):
            return "blocked"
        if any(m in lowered for m in _CAPTCHA_MARKERS):
            return "captcha"
        return "empty"  # 200 but no data and no obvious captcha — treat as soft fail

    def _dump_debug(self, tag: str, html: str) -> None:
        if not self._debug_dir:
            return
        from pathlib import Path

        d = Path(self._debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tag}.html").write_text(html, encoding="utf-8")
        try:
            self.page.screenshot(path=str(d / f"{tag}.png"), full_page=False)
        except Exception:  # pragma: no cover
            pass
        logger.info("saved diagnostics to %s.{html,png}", (d / tag))

    def _backoff(self, attempt: int) -> None:
        delay = self.rate.backoff_base_seconds * (2 ** (attempt - 1))
        delay += random.uniform(0, self.rate.backoff_base_seconds)
        logger.warning("backing off %.1fs (attempt %d/%d)", delay, attempt, self.rate.max_retries)
        time.sleep(delay)

    # --- public surface (matches ZillowClient) ------------------------------

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        """Navigate to a page (running its JS) and return the rendered HTML."""
        full = url + ("?" + urlencode(params) if params else "")
        last_status: int | None = None
        last_verdict = "?"
        for attempt in range(1, self.rate.max_retries + 1):
            try:
                resp = self.page.goto(full, wait_until="domcontentloaded", timeout=45000)
                last_status = resp.status if resp else None
                self._settle()
                html = self.page.content()
                title = self.page.title()
            except Exception as exc:  # navigation timeout / transient browser error
                logger.warning("navigation error on %s: %s", full, exc)
                self._backoff(attempt)
                continue
            last_verdict = self._classify(last_status, html)
            logger.info("status=%s verdict=%s title=%r", last_status, last_verdict, title)
            if last_verdict == "ok":
                self._on_zillow = True
                self._pace()
                return html
            logger.warning("not usable (status %s, %s) on %s — attempt %d/%d",
                           last_status, last_verdict, full, attempt, self.rate.max_retries)
            self._dump_debug(f"block_{last_verdict}_attempt{attempt}", html)
            self._backoff(attempt)
        raise BlockedError(
            f"Zillow returned no results for {full} after {self.rate.max_retries} "
            f"attempts (last status {last_status}, verdict {last_verdict})."
        )

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a JSON endpoint from within the page (carries browser cookies)."""
        if not self._on_zillow:
            # Must be on a zillow.com origin for the same-origin fetch to carry cookies.
            self.get_text("https://www.zillow.com/")
        full = url + ("?" + urlencode(params) if params else "")
        result = self.page.evaluate(
            """async (u) => {
                const r = await fetch(u, {
                    headers: {
                        'accept': 'application/json,text/javascript,*/*;q=0.01',
                        'x-requested-with': 'XMLHttpRequest'
                    },
                    credentials: 'include'
                });
                return { status: r.status, body: await r.text() };
            }""",
            full,
        )
        status = result.get("status")
        body = result.get("body", "")
        if status != 200 or self._classify(status, body) != "ok":
            self._dump_debug("api_block", body)
            raise BlockedError(f"Zillow API returned {status} for {url}")
        self._pace()
        return json.loads(body)

    def close(self) -> None:
        for closeable in (getattr(self, "context", None), getattr(self, "browser", None)):
            try:
                if closeable is not None:
                    closeable.close()
            except Exception:  # pragma: no cover
                pass
        try:
            self._pw.stop()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> "PlaywrightZillowClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
