"""Build and execute Zillow searches for a ZIP code.

Flow per ZIP:
  1. Fetch the search results HTML page for the ZIP and extract the embedded
     ``searchQueryState`` (carries the map bounds Zillow resolved for the ZIP).
  2. Page through results via the ``GetSearchPageState.htm`` JSON endpoint,
     reusing that query state and bumping ``pagination.currentPage``.
  3. Normalize each result with ``parse.extract_listings``.

The HTML-embedded state can shift as Zillow changes its frontend; extraction is
defensive and failures degrade to "skip this ZIP and log", never a crash.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ...config import Filters
from . import parse
from .parse import Listing
from .zillow_client import BlockedError, ZillowClient

logger = logging.getLogger(__name__)

SEARCH_PAGE_STATE_URL = "https://www.zillow.com/search/GetSearchPageState.htm"

# Matches the embedded query-state JSON object inside the search results HTML.
_QUERY_STATE_RE = re.compile(r'"queryState":\s*(\{.*?\})\s*,\s*"(?:cat1|wants|isDebug)"')


def zip_search_url(zipcode: str) -> str:
    return f"https://www.zillow.com/homes/for_sale/{zipcode}_rb/"


def extract_query_state_from_html(html: str) -> dict[str, Any] | None:
    """Best-effort extraction of the embedded searchQueryState from search HTML."""
    match = _QUERY_STATE_RE.search(html)
    if not match:
        # Fallback: locate the mapBounds object and walk outward to a JSON object.
        idx = html.find('"mapBounds"')
        if idx == -1:
            return None
        start = html.rfind("{", 0, idx)
        depth, end = 0, -1
        for i in range(start, len(html)):
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return None
        snippet = html[start:end]
    else:
        snippet = match.group(1)

    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        logger.warning("could not JSON-decode embedded query state")
        return None


def apply_filters(query_state: dict[str, Any], filters: Filters) -> dict[str, Any]:
    """Overlay config filters onto the query state's filterState."""
    fs: dict[str, Any] = query_state.setdefault("filterState", {})
    if filters.status == "for_sale":
        fs.setdefault("isForSaleByAgent", {"value": True})
        fs["isForRent"] = {"value": False}
    elif filters.status == "for_rent":
        fs["isForRent"] = {"value": True}
        fs["isForSaleByAgent"] = {"value": False}

    if filters.min_price is not None or filters.max_price is not None:
        fs["price"] = {
            k: v
            for k, v in (("min", filters.min_price), ("max", filters.max_price))
            if v is not None
        }
    if filters.min_beds is not None or filters.max_beds is not None:
        fs["beds"] = {
            k: v
            for k, v in (("min", filters.min_beds), ("max", filters.max_beds))
            if v is not None
        }
    return query_state


def fetch_page_state(
    client: ZillowClient,
    query_state: dict[str, Any],
    page: int,
    *,
    referer: str | None = None,
) -> dict[str, Any]:
    """Call GetSearchPageState for a given page using the query state."""
    qs = json.loads(json.dumps(query_state))  # deep copy
    qs.setdefault("pagination", {})["currentPage"] = page
    wants = {"cat1": ["listResults", "mapResults"], "cat2": ["total"]}
    params = {
        "searchQueryState": json.dumps(qs, separators=(",", ":")),
        "wants": json.dumps(wants, separators=(",", ":")),
        "requestId": page + 1,
    }
    return client.get_json(SEARCH_PAGE_STATE_URL, params=params, referer=referer)


def scrape_zip(
    client: ZillowClient, zipcode: str, filters: Filters
) -> list[Listing]:
    """Scrape up to ``filters.max_pages`` of listings for one ZIP code."""
    logger.info("scraping ZIP %s", zipcode)
    search_url = zip_search_url(zipcode)
    html = client.get_text(search_url)
    query_state = extract_query_state_from_html(html)
    if query_state is None:
        logger.error("ZIP %s: could not extract search state — skipping", zipcode)
        return []

    apply_filters(query_state, filters)

    collected: dict[str, Listing] = {}
    for page in range(1, filters.max_pages + 1):
        try:
            page_state = fetch_page_state(client, query_state, page, referer=search_url)
        except BlockedError as exc:
            logger.error("ZIP %s page %d blocked: %s", zipcode, page, exc)
            break
        listings = parse.extract_listings(page_state)
        if not listings:
            logger.info("ZIP %s page %d: no more results", zipcode, page)
            break
        for listing in listings:
            collected.setdefault(listing.zpid, listing)
        logger.info("ZIP %s page %d: %d listings", zipcode, page, len(listings))

    logger.info("ZIP %s: %d unique listings", zipcode, len(collected))
    return list(collected.values())
