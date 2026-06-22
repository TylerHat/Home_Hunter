"""Scraping backends. The active source is Craigslist (NYC rentals).

``build_client`` returns the HTTP client for the configured source. The pipeline
and DB only depend on this seam, so adding another source later is local.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config
    from .craigslist.client import CraigslistClient


def build_client(config: "Config") -> "CraigslistClient":
    """Construct the HTTP client for the configured source."""
    if config.source == "craigslist":
        from .craigslist.client import CraigslistClient

        return CraigslistClient(config.rate_limit)
    if config.source == "zillow":
        raise ValueError(
            "the 'zillow' source is legacy and not wired into the pipeline; "
            "see src/home_hunter/scraper/zillow/ to revive it."
        )
    raise ValueError(f"unknown source {config.source!r} (expected 'craigslist')")
