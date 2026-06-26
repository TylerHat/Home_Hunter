"""Scraping backends. Active sources are Craigslist and RentHop (NYC rentals).

``build_client`` returns the HTTP client for the configured source and
``search_area`` dispatches to that source's scraper. The pipeline and DB depend
only on these two seams plus the shared ``RentalListing`` model, so adding
another source stays local to a new ``scraper/<source>/`` package.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

# Re-exported so callers do ``from ..scraper import RentalListing`` rather than
# reaching into a specific source package. The model lives in the Craigslist
# parser and is shared by every source (each just sets a different ``source``).
from .craigslist.parse import RentalListing

if TYPE_CHECKING:
    from ..config import Config
    from .craigslist.client import CraigslistClient
    from .renthop.client import RentHopClient

# Every wired source, in the order the "Rescan all listings" sweep pulls them:
# Craigslist first, then RentHop, then any future source appended here. (A normal
# CLI run still scrapes the single `source` set in config.yaml; this list is only
# the rescan's full sweep.)
ACTIVE_SOURCES: tuple[str, ...] = ("craigslist", "renthop")

__all__ = ["ACTIVE_SOURCES", "RentalListing", "build_client", "search_area"]


def build_client(config: "Config") -> "CraigslistClient | RentHopClient":
    """Construct the HTTP client for the configured source."""
    if config.source == "craigslist":
        from .craigslist.client import CraigslistClient

        return CraigslistClient(config.rate_limit)
    if config.source == "renthop":
        from .renthop.client import RentHopClient

        return RentHopClient(config.rate_limit)
    if config.source == "zillow":
        raise ValueError(
            "the 'zillow' source is legacy and not wired into the pipeline; "
            "see src/home_hunter/scraper/zillow/ to revive it."
        )
    raise ValueError(
        f"unknown source {config.source!r} (expected 'craigslist' or 'renthop')"
    )


def search_area(
    client: object,
    config: "Config",
    area: str,
    on_event: Callable[[dict], None] | None = None,
) -> list[RentalListing]:
    """Scrape one area, dispatching to the configured source's ``search_area``."""
    if config.source == "renthop":
        from .renthop.search import search_area as _search_area
    else:
        from .craigslist.search import search_area as _search_area
    return _search_area(client, config, area, on_event=on_event)
