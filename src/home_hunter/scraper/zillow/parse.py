"""Normalize Zillow's GetSearchPageState JSON into clean Listing models.

The search endpoint returns results under
``cat1.searchResults.listResults`` (and ``mapResults``). Each result carries a
rich ``hdpData.homeInfo`` block which is our preferred structured source, with
top-level fields as fallback.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator

ZILLOW_BASE = "https://www.zillow.com"


class Listing(BaseModel):
    """One normalized Zillow for-sale listing."""

    zpid: str
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    living_area_sqft: int | None = None
    lot_size: float | None = None
    lot_size_unit: str | None = None
    home_type: str | None = None
    status: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    url: str | None = None
    raw: dict[str, Any] = {}

    @field_validator("zpid", mode="before")
    @classmethod
    def _coerce_zpid(cls, v: Any) -> str:
        return str(v).strip()


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _full_url(detail_url: str | None) -> str | None:
    if not detail_url:
        return None
    if detail_url.startswith("http"):
        return detail_url
    return f"{ZILLOW_BASE}{detail_url}"


def parse_listing(result: dict[str, Any]) -> Listing | None:
    """Parse a single ``listResults`` item. Returns None if it has no zpid."""
    zpid = result.get("zpid") or result.get("id")
    if zpid is None:
        return None

    info: dict[str, Any] = result.get("hdpData", {}).get("homeInfo", {}) or {}
    lat_long: dict[str, Any] = result.get("latLong", {}) or {}

    return Listing(
        zpid=zpid,
        street_address=info.get("streetAddress") or result.get("addressStreet"),
        city=info.get("city") or result.get("addressCity"),
        state=info.get("state") or result.get("addressState"),
        zipcode=info.get("zipcode") or result.get("addressZipcode"),
        price=_to_int(info.get("price") or result.get("unformattedPrice")),
        beds=_to_float(info.get("bedrooms") or result.get("beds")),
        baths=_to_float(info.get("bathrooms") or result.get("baths")),
        living_area_sqft=_to_int(info.get("livingArea") or result.get("area")),
        lot_size=_to_float(info.get("lotAreaValue")),
        lot_size_unit=info.get("lotAreaUnit"),
        home_type=info.get("homeType"),
        status=info.get("homeStatus") or result.get("statusType"),
        latitude=_to_float(info.get("latitude") or lat_long.get("latitude")),
        longitude=_to_float(info.get("longitude") or lat_long.get("longitude")),
        url=_full_url(result.get("detailUrl")),
        raw=result,
    )


def extract_listings(page_state: dict[str, Any]) -> list[Listing]:
    """Pull and normalize all listings from a GetSearchPageState response.

    De-duplicates by zpid (a result can appear in both list and map results).
    """
    search_results = (page_state.get("cat1", {}) or {}).get("searchResults", {}) or {}
    raw_results: list[dict[str, Any]] = []
    raw_results.extend(search_results.get("listResults", []) or [])
    raw_results.extend(search_results.get("mapResults", []) or [])

    listings: dict[str, Listing] = {}
    for result in raw_results:
        listing = parse_listing(result)
        if listing is not None and listing.zpid not in listings:
            listings[listing.zpid] = listing
    return list(listings.values())


def total_result_count(page_state: dict[str, Any]) -> int | None:
    """Best-effort total match count reported by the search response."""
    cat1 = page_state.get("cat1", {}) or {}
    totals = page_state.get("categoryTotals", {}) or {}
    return (
        _to_int(cat1.get("searchList", {}).get("totalResultCount"))
        or _to_int(totals.get("cat1", {}).get("totalResultCount"))
    )
