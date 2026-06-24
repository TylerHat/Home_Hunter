"""Build and execute Craigslist NYC rental searches for one borough.

Flow per area (borough):
  1. Page through ``/search/<area>/apa`` (apartments / housing for rent),
     reading the summaries embedded in each results page.
  2. For each summary, optionally fetch its detail page to capture square
     footage + amenities, then normalize to a ``RentalListing``.
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

from ...config import Config, Filters
from .client import BlockedError, CraigslistClient
from .parse import (
    RentalListing,
    RentalSummary,
    parse_detail,
    parse_search_results,
    plausible_rent,
)

logger = logging.getLogger(__name__)

# Craigslist returns search results in batches paged by a `s` (start) offset.
RESULTS_PER_PAGE = 120


def search_path_url(city: str, area: str) -> str:
    """Base search URL for apartments/housing for rent in a Craigslist area."""
    return f"https://{city}.craigslist.org/search/{area}/apa"


def build_params(filters: Filters, offset: int = 0) -> dict[str, object]:
    """Translate rental filters into Craigslist search query params."""
    params: dict[str, object] = {}
    if filters.min_rent is not None:
        params["min_price"] = filters.min_rent
    if filters.max_rent is not None:
        params["max_price"] = filters.max_rent
    if filters.min_beds is not None:
        params["min_bedrooms"] = filters.min_beds
    if filters.max_beds is not None:
        params["max_bedrooms"] = filters.max_beds
    if filters.cats_ok:
        params["pets_cat"] = 1
    if filters.dogs_ok:
        params["pets_dog"] = 1
    if offset:
        params["s"] = offset
    return params


def collect_summaries(
    client: CraigslistClient, city: str, area: str, filters: Filters
) -> list[RentalSummary]:
    """Page through search results, de-duplicating by pid, up to max_pages."""
    base_url = search_path_url(city, area)
    cap = max(filters.max_pages, 1) * RESULTS_PER_PAGE
    collected: dict[str, RentalSummary] = {}

    for page in range(max(filters.max_pages, 1)):
        params = build_params(filters, offset=page * RESULTS_PER_PAGE)
        try:
            html = client.get_text(base_url, params=params)
        except BlockedError as exc:
            logger.error("%s page %d blocked: %s", area, page + 1, exc)
            break
        rows = parse_search_results(html)
        new = [r for r in rows if r.pid not in collected]
        for r in new:
            collected[r.pid] = r
        logger.info(
            "%s page %d: %d rows (%d new, %d total)",
            area, page + 1, len(rows), len(new), len(collected),
        )
        if not new or len(collected) >= cap:
            break

    return list(collected.values())[:cap]


def _summary_listing(summary: RentalSummary, borough: str | None) -> RentalListing:
    """A RentalListing from search-page fields only (no detail page)."""
    return RentalListing(
        pid=summary.pid,
        url=summary.url,
        title=summary.title,
        neighborhood=summary.neighborhood,
        price=summary.price,
        borough=borough,
        raw={"summary": summary.model_dump()},
    )


def enrich(
    client: CraigslistClient, summary: RentalSummary, borough: str | None
) -> RentalListing:
    """Fetch a summary's detail page and merge it; fall back to summary-only.

    A single dead/blocked detail page (e.g. ``410 Gone`` when a listing is pulled
    between the search and detail fetch, or a transient network error) must never
    abort the whole borough — degrade to the summary-only listing instead.
    """
    try:
        detail_html = client.get_text(summary.url)
    except (BlockedError, httpx.HTTPError) as exc:
        logger.warning(
            "detail %s unavailable: %s — keeping summary only", summary.pid, exc
        )
        return _summary_listing(summary, borough)
    return parse_detail(detail_html, summary, borough=borough)


def fetch_details(
    client: CraigslistClient,
    summaries: list[RentalSummary],
    config: Config,
    borough: str | None,
    make_client: Callable[[], CraigslistClient],
) -> list[RentalListing]:
    """Enrich summaries via their detail pages, parallelizing politely.

    With ``detail_concurrency`` > 1, detail pages are fetched through a small
    pool of clients (each keeping its own pacing) so wall-time drops ~Nx while
    every connection still looks human-paced. ``make_client`` is the seam tests
    use to inject offline fakes.
    """
    workers = max(1, config.rate_limit.detail_concurrency)
    if workers == 1 or len(summaries) <= 1:
        return [enrich(client, s, borough) for s in summaries]

    pool: queue.Queue[CraigslistClient] = queue.Queue()
    created = [make_client() for _ in range(workers)]
    for c in created:
        pool.put(c)

    def task(summary: RentalSummary) -> RentalListing:
        c = pool.get()
        try:
            return enrich(c, summary, borough)
        finally:
            pool.put(c)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(task, summaries))
    finally:
        for c in created:
            c.close()


def search_area(
    client: CraigslistClient,
    config: Config,
    area: str,
) -> list[RentalListing]:
    """Scrape one borough: collect summaries, then enrich via detail pages."""
    borough = config.area_name(area)
    logger.info("scraping %s (%s)", borough, area)
    summaries = collect_summaries(client, config.city, area, config.filters)

    if not config.detail_fetch:
        listings = [_summary_listing(s, borough) for s in summaries]
    else:
        listings = fetch_details(
            client,
            summaries,
            config,
            borough,
            make_client=lambda: CraigslistClient(config.rate_limit),
        )

    kept = [r for r in listings if plausible_rent(r.price)]
    dropped = len(listings) - len(kept)
    if dropped:
        logger.info("%s: dropped %d listing(s) with implausible rent", borough, dropped)
    logger.info("%s: %d listings", borough, len(kept))
    return kept
