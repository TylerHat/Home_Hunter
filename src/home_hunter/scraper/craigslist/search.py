"""Build and execute Craigslist NYC rental searches for one borough.

Flow per area (borough):
  1. Page through ``/search/<area>/apa`` (apartments / housing for rent),
     reading the summaries embedded in each results page.
  2. For each summary, optionally fetch its detail page to capture square
     footage + amenities, then normalize to a ``RentalListing``.
"""

from __future__ import annotations

import logging

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


def search_area(
    client: CraigslistClient,
    config: Config,
    area: str,
) -> list[RentalListing]:
    """Scrape one borough: collect summaries, then enrich via detail pages."""
    borough = config.area_name(area)
    logger.info("scraping %s (%s)", borough, area)
    summaries = collect_summaries(client, config.city, area, config.filters)

    listings: list[RentalListing] = []
    for summary in summaries:
        if not config.detail_fetch:
            listings.append(
                RentalListing(
                    pid=summary.pid,
                    url=summary.url,
                    title=summary.title,
                    neighborhood=summary.neighborhood,
                    price=summary.price,
                    borough=borough,
                    raw={"summary": summary.model_dump()},
                )
            )
            continue
        try:
            detail_html = client.get_text(summary.url)
        except BlockedError as exc:
            logger.warning("detail %s blocked: %s — keeping summary only", summary.pid, exc)
            listings.append(
                RentalListing(
                    pid=summary.pid,
                    url=summary.url,
                    title=summary.title,
                    neighborhood=summary.neighborhood,
                    price=summary.price,
                    borough=borough,
                    raw={"summary": summary.model_dump()},
                )
            )
            continue
        listings.append(parse_detail(detail_html, summary, borough=borough))

    kept = [r for r in listings if plausible_rent(r.price)]
    dropped = len(listings) - len(kept)
    if dropped:
        logger.info("%s: dropped %d listing(s) with implausible rent", borough, dropped)
    logger.info("%s: %d listings", borough, len(kept))
    return kept
