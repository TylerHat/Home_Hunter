"""Offline tests for concurrent detail fetching — no network required.

These drive ``fetch_details`` / ``enrich`` with fake clients that serve a
fixture instead of making HTTP calls, proving the concurrent path enriches
every summary, preserves order, closes its pooled clients, and degrades to a
summary-only listing when a detail page is blocked.
"""

from pathlib import Path

import httpx

from home_hunter.config import Config, RateLimit
from home_hunter.scraper.craigslist import BlockedError
from home_hunter.scraper.craigslist.parse import RentalSummary
from home_hunter.scraper.craigslist.search import enrich, fetch_details

FIXTURES = Path(__file__).parent / "fixtures"
DETAIL_HTML = (FIXTURES / "craigslist_detail.html").read_text(encoding="utf-8")


class FakeClient:
    """Stand-in for CraigslistClient that serves a fixture instead of HTTP."""

    def __init__(self, html: str = DETAIL_HTML, *, blocked: bool = False) -> None:
        self.html = html
        self.blocked = blocked
        self.calls: list[str] = []
        self.closed = False

    def get_text(self, url, params=None):
        self.calls.append(url)
        if self.blocked:
            raise BlockedError("blocked")
        return self.html

    def close(self):
        self.closed = True


class _GoneClient(FakeClient):
    """A client whose detail fetches always return 410 Gone."""

    def get_text(self, url, params=None):
        self.calls.append(url)
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "gone", request=req, response=httpx.Response(410, request=req)
        )


def _summaries(n: int) -> list[RentalSummary]:
    return [
        RentalSummary(
            pid=str(i),
            url=f"https://newyork.craigslist.org/mnh/apa/d/x/{i}.html",
            title=f"listing {i}",
            price=2000 + i,
        )
        for i in range(n)
    ]


def _config(concurrency: int) -> Config:
    return Config(rate_limit=RateLimit(detail_concurrency=concurrency))


def test_fetch_details_concurrent_enriches_all_in_order():
    summaries = _summaries(5)
    created: list[FakeClient] = []

    def make_client() -> FakeClient:
        c = FakeClient()
        created.append(c)
        return c

    shared = FakeClient()
    listings = fetch_details(
        shared, summaries, _config(3), "Manhattan", make_client=make_client
    )

    # Order preserved, every summary enriched with detail-only fields.
    assert [r.pid for r in listings] == [s.pid for s in summaries]
    assert all(r.beds == 1.0 for r in listings)  # beds come from the detail page
    assert all(r.borough == "Manhattan" for r in listings)
    # Concurrent path uses a pool of fresh clients (not the shared one) and closes them.
    assert shared.calls == []
    assert len(created) == 3  # one client per worker
    assert all(c.closed for c in created)


def test_fetch_details_sequential_uses_shared_client():
    summaries = _summaries(3)

    def make_client() -> FakeClient:
        raise AssertionError("sequential path must not create extra clients")

    shared = FakeClient()
    listings = fetch_details(
        shared, summaries, _config(1), "Queens", make_client=make_client
    )

    assert [r.pid for r in listings] == [s.pid for s in summaries]
    assert len(shared.calls) == 3  # all fetched on the passed-in client
    assert all(r.beds == 1.0 for r in listings)


def test_blocked_detail_falls_back_to_summary():
    summary = _summaries(1)[0]
    listing = enrich(FakeClient(blocked=True), summary, "Bronx")

    # Summary-only: detail fields absent, but the cheap search-page fields survive.
    assert listing.pid == summary.pid
    assert listing.price == summary.price
    assert listing.title == summary.title
    assert listing.borough == "Bronx"
    assert listing.beds is None  # never reached the detail page


def test_http_error_detail_falls_back_to_summary():
    # A 410 Gone (listing pulled between search and detail) must degrade, not raise.
    summary = _summaries(1)[0]
    listing = enrich(_GoneClient(), summary, "Bronx")
    assert listing.pid == summary.pid
    assert listing.price == summary.price
    assert listing.beds is None  # detail page never parsed


def test_concurrent_one_dead_detail_does_not_abort_batch():
    # Regression: in the concurrent path, one bad detail page used to propagate
    # out of executor.map and kill the entire borough. It must not.
    summaries = _summaries(6)
    dead = {"2", "4"}

    class FlakyClient(FakeClient):
        def get_text(self, url, params=None):
            self.calls.append(url)
            if any(f"/{p}.html" in url for p in dead):
                req = httpx.Request("GET", url)
                raise httpx.HTTPStatusError(
                    "gone", request=req, response=httpx.Response(410, request=req)
                )
            return self.html

    def make_client() -> FlakyClient:
        return FlakyClient()

    listings = fetch_details(
        FakeClient(), summaries, _config(3), "Brooklyn", make_client=make_client
    )

    # All six come back, in order — none lost to the dead pages.
    assert [r.pid for r in listings] == [s.pid for s in summaries]
    by_pid = {r.pid: r for r in listings}
    assert by_pid["2"].beds is None and by_pid["4"].beds is None  # dead -> summary-only
    assert by_pid["0"].beds == 1.0  # live -> enriched
