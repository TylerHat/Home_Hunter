"""Craigslist NYC rental scraping: HTTP client, search, and HTML parsing."""

from __future__ import annotations

from .client import BlockedError, CraigslistClient
from .parse import RentalListing, RentalSummary, parse_detail, parse_search_results
from .search import search_area

__all__ = [
    "BlockedError",
    "CraigslistClient",
    "RentalListing",
    "RentalSummary",
    "parse_detail",
    "parse_search_results",
    "search_area",
]
