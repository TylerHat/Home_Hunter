"""Parse RentHop NYC search pages into the shared ``RentalListing`` model.

RentHop renders results as HTML cards (no clean JSON blob), each a sibling
``<div class="... search-listing ..." id="listing-<id>" listing_id='<id>'
latitude='..' longitude='..'>``. A card carries everything Home Hunter needs
without a detail fetch: the address/title, the neighborhoods line (whose last
comma-segment is the borough), zip, price, a "No Fee" badge, and the
beds/baths/sqft figures next to their icons.

Extraction is regex-based and defensive — like the Craigslist parser, missing
fields degrade to ``None``/``False`` instead of raising, because RentHop markup
shifts over time. The model, the price/text helpers, and the NYC text signals
(no-fee, rent-stabilized) are reused from the Craigslist parser so both sources
produce identical ``RentalListing`` rows distinguished only by ``source``.
"""

from __future__ import annotations

import re

from ..craigslist.parse import (
    _NOFEE_RE,
    _RENT_STABILIZED_RE,
    _clean,
    _price_to_int,
    RentalListing,
)

SOURCE = "renthop"

# Opening <div> of one listing card: capture id, latitude, longitude. The
# `listing_id='..'` attribute sits between id and latitude, skipped by `[^>]*`.
_CARD_OPEN_RE = re.compile(
    r'<div[^>]*\bclass="[^"]*\bsearch-listing\b[^"]*"[^>]*'
    r'\bid="listing-(\d+)"[^>]*'
    r"\blatitude='([^']*)'[^>]*"
    r"\blongitude='([^']*)'",
    re.I,
)
_LISTING_URL_RE = re.compile(r'href="(https://www\.renthop\.com/listings/[^"]+)"', re.I)
_BEDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bed", re.I)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.I)
_SQFT_RE = re.compile(r"([\d,]+)\s*Sqft", re.I)
_STUDIO_RE = re.compile(r"\bStudio\b", re.I)


def _field(listing_id: str, suffix: str, card: str) -> str | None:
    """Text of a ``id="listing-<id>-<suffix>"`` element (id may use ' or \")."""
    m = re.search(
        rf"""id=['"]listing-{listing_id}-{suffix}['"][^>]*>(.*?)</""",
        card,
        re.S | re.I,
    )
    return _clean(m.group(1)) if m else None


def _float(pattern: re.Pattern[str], text: str) -> float | None:
    m = pattern.search(text)
    return float(m.group(1)) if m else None


def parse_search_results(
    page_html: str, *, borough: str | None = None
) -> list[RentalListing]:
    """Extract every listing card from a RentHop search-results page.

    ``borough`` is the fallback borough for the searched area; when a card's own
    neighborhoods line names a borough (its last comma-segment) that is used
    instead, since it is per-listing accurate.
    """
    starts = [(m.start(), m) for m in _CARD_OPEN_RE.finditer(page_html)]
    listings: dict[str, RentalListing] = {}
    for i, (pos, m) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(page_html)
        card = page_html[pos:end]
        lid = m.group(1)
        pid = f"rh-{lid}"
        if pid in listings:
            continue

        lat = _safe_float(m.group(2))
        lng = _safe_float(m.group(3))

        url_m = _LISTING_URL_RE.search(card)
        url = url_m.group(1).split("?", 1)[0] if url_m else None

        title = _field(lid, "title", card)
        neighborhoods = _field(lid, "neighborhoods", card)
        neighborhood = card_borough = None
        if neighborhoods:
            parts = [p.strip() for p in neighborhoods.split(",") if p.strip()]
            if parts:
                neighborhood = parts[0]
                card_borough = parts[-1]

        price = _price_to_int(_field(lid, "price", card))

        beds = _float(_BEDS_RE, card)
        if beds is None and _STUDIO_RE.search(card):
            beds = 0.0
        baths = _float(_BATHS_RE, card)
        sqft_m = _SQFT_RE.search(card)
        sqft = _price_to_int(sqft_m.group(1)) if sqft_m else None

        text_blob = f"{title or ''} {neighborhoods or ''}"
        listings[pid] = RentalListing(
            pid=pid,
            source=SOURCE,
            url=url,
            title=title,
            neighborhood=neighborhood,
            borough=card_borough or borough,
            price=price,
            beds=beds,
            baths=baths,
            sqft=sqft,
            no_fee=bool(_NOFEE_RE.search(card)),
            rent_stabilized=bool(_RENT_STABILIZED_RE.search(text_blob)),
            latitude=lat,
            longitude=lng,
            raw={"listing_id": lid, "neighborhoods": neighborhoods},
        )
    return list(listings.values())


def _safe_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
