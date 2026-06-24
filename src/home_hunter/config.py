"""Load scrape configuration from config.yaml and environment variables.

Home Hunter targets **NYC apartment rentals** via Craigslist. Areas are
Craigslist sub-region codes (one per borough); filters are rental filters
(rent, beds, pets) applied to the Craigslist search.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # read .env if present (no-op in CI where env is set directly)

# Craigslist NYC sub-region codes -> display borough name.
NYC_AREAS: dict[str, str] = {
    "mnh": "Manhattan",
    "brk": "Brooklyn",
    "que": "Queens",
    "brx": "Bronx",
    "stn": "Staten Island",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Filters:
    """Rental search filters applied to the Craigslist query."""

    min_rent: int | None = None
    max_rent: int | None = None
    min_beds: int | None = None
    max_beds: int | None = None
    cats_ok: bool | None = None   # True -> only cat-friendly listings
    dogs_ok: bool | None = None   # True -> only dog-friendly listings
    max_pages: int = 3            # search pages per area (~120 results/page)


@dataclass(frozen=True)
class RateLimit:
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    max_retries: int = 3
    backoff_base_seconds: float = 4.0
    user_agent: str = DEFAULT_USER_AGENT
    # Detail pages fetched in parallel. Each worker keeps its own min/max
    # pacing, so concurrency N ~= N x the aggregate request rate. Keep modest.
    detail_concurrency: int = 4


@dataclass(frozen=True)
class Config:
    # Craigslist site subdomain (newyork.craigslist.org -> "newyork").
    city: str = "newyork"
    # Craigslist sub-region codes to scrape (NYC boroughs by default).
    areas: list[str] = field(default_factory=lambda: list(NYC_AREAS))
    filters: Filters = field(default_factory=Filters)
    rate_limit: RateLimit = field(default_factory=RateLimit)
    # Data source. "craigslist" is the active backend; "zillow" is legacy
    # (kept under scraper/zillow/ but not wired into the default pipeline).
    source: str = "craigslist"
    # Open each listing's detail page to capture sqft + amenities. Disabling
    # keeps only the cheap search-page fields (price, beds, neighborhood).
    detail_fetch: bool = True

    @property
    def database_url(self) -> str:
        """Postgres/Neon URL if set, else a local SQLite file."""
        url = os.getenv("DATABASE_URL", "").strip()
        if url:
            return url
        db_path = Path.cwd() / "home_hunter.db"
        return f"sqlite+pysqlite:///{db_path.as_posix()}"

    def area_name(self, area: str) -> str:
        """Human-readable borough name for a Craigslist area code."""
        return NYC_AREAS.get(area, area)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load Config from a YAML file (defaults to $HOME_HUNTER_CONFIG or ./config.yaml)."""
    cfg_path = Path(
        path or os.getenv("HOME_HUNTER_CONFIG", "config.yaml")
    ).expanduser()

    data: dict = {}
    if cfg_path.is_file():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    areas = [str(a).strip() for a in (data.get("areas") or list(NYC_AREAS))]
    filters = Filters(**(data.get("filters") or {}))
    rate_limit = RateLimit(**(data.get("rate_limit") or {}))

    return Config(
        city=str(data.get("city", "newyork")).strip(),
        areas=areas,
        filters=filters,
        rate_limit=rate_limit,
        source=str(data.get("source", "craigslist")).strip(),
        detail_fetch=bool(data.get("detail_fetch", True)),
    )
