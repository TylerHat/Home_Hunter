"""Build and execute RentHop NYC rental searches for one borough.

RentHop exposes a per-borough listing index at
``/apartments-for-rent/<slug>`` paged by a ``page`` query param, with each
results page already carrying the full listing cards (price, beds/baths/sqft,
address, lat/long, no-fee). Unlike Craigslist there is **no detail-page fetch**:
the card has everything ``RentalListing`` needs, which keeps request volume — and
Cloudflare exposure — minimal.

``search_area`` matches the Craigslist signature so the pipeline dispatches to
either source unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ...config import Config, Filters
from ..craigslist.parse import RentalListing, plausible_rent
from .client import BlockedError, RentHopClient
from .parse import parse_search_results

logger = logging.getLogger(__name__)

# Home Hunter borough codes -> RentHop area slug. Confirmed against the live site
# (the short ``brooklyn-ny`` forms 404 to an empty index; the city-qualified
# ``<borough>-new-york-ny`` forms are the real per-borough pages).
_AREA_SLUGS: dict[str, str] = {
    "mnh": "manhattan-new-york-ny",
    "brk": "brooklyn-new-york-ny",
    "que": "queens-new-york-ny",
    "brx": "bronx-new-york-ny",
    "stn": "staten-island-new-york-ny",
}


def search_url(slug: str) -> str:
    return f"https://www.renthop.com/apartments-for-rent/{slug}"


def build_params(filters: Filters, page: int) -> dict[str, object]:
    """RentHop query params. Only price + paging are sent on the URL (their names
    are confirmed); bed bounds are applied client-side after parsing."""
    params: dict[str, object] = {"page": page, "sort": "hopscore"}
    if filters.min_rent is not None:
        params["min_price"] = filters.min_rent
    if filters.max_rent is not None:
        params["max_price"] = filters.max_rent
    return params


def _matches_beds(listing: RentalListing, filters: Filters) -> bool:
    """Apply min/max bed bounds client-side (RentHop bed params aren't confirmed)."""
    if listing.beds is None:
        return True  # don't drop unknown-bed listings
    if filters.min_beds is not None and listing.beds < filters.min_beds:
        return False
    if filters.max_beds is not None and listing.beds > filters.max_beds:
        return False
    return True


def search_area(
    client: RentHopClient,
    config: Config,
    area: str,
    on_event: Callable[[dict], None] | None = None,
) -> list[RentalListing]:
    """Scrape one borough's RentHop index across up to ``max_pages`` pages.

    ``on_event`` (optional) mirrors the Craigslist contract for the in-UI rescan:
    one ``{"type": "summaries", "count": n}`` event with the borough total, then
    one ``{"type": "listing"}`` per kept listing.
    """
    borough = config.area_name(area)
    slug = _AREA_SLUGS.get(area)
    if slug is None:
        logger.warning("renthop: no area slug for %r — skipping", area)
        if on_event is not None:
            on_event({"type": "summaries", "count": 0})
        return []

    url = search_url(slug)
    filters = config.filters
    collected: dict[str, RentalListing] = {}
    for page in range(1, max(filters.max_pages, 1) + 1):
        try:
            html = client.get_text(url, params=build_params(filters, page))
        except BlockedError as exc:
            logger.error("renthop %s page %d blocked: %s", area, page, exc)
            break
        rows = parse_search_results(html, borough=borough)
        new = [
            r
            for r in rows
            if r.pid not in collected and plausible_rent(r.price) and _matches_beds(r, filters)
        ]
        for r in new:
            collected[r.pid] = r
        logger.info(
            "renthop %s page %d: %d cards (%d kept, %d total)",
            area, page, len(rows), len(new), len(collected),
        )
        if not rows:  # ran past the last page
            break

    listings = list(collected.values())
    if on_event is not None:
        on_event({"type": "summaries", "count": len(listings)})
        for _ in listings:
            on_event({"type": "listing"})
    logger.info("renthop %s: %d listings", borough, len(listings))
    return listings
