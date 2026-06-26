"""RentHop NYC rental scraping: curl_cffi client, search, and HTML parsing.

RentHop sits behind Cloudflare, so the client uses curl_cffi Chrome TLS
impersonation (no headless browser). Listings are reused as the shared
``RentalListing`` from the Craigslist parser, distinguished only by ``source``.
"""

from __future__ import annotations

from .client import BlockedError, RentHopClient
from .parse import parse_search_results
from .search import search_area

__all__ = [
    "BlockedError",
    "RentHopClient",
    "parse_search_results",
    "search_area",
]
