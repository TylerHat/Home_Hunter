"""Parse Craigslist NYC rental search + detail pages into clean models.

Two stages:
  * ``parse_search_results(html)`` -> list[RentalSummary] from a search page.
    Cheap fields only: pid, url, title, price, neighborhood.
  * ``parse_detail(html, summary)`` -> RentalListing with square footage and
    amenities. Craigslist encodes structured attributes as query-param links
    (e.g. ``...?laundry=2`` -> "laundry in bldg"), which we read by key + text.

Extraction is regex-based and defensive: missing fields degrade to ``None`` /
``False`` rather than raising, because Craigslist markup shifts over time.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

# Maps a Craigslist attr query-param key to (field_name, is_flag).
# is_flag=True -> presence means True; is_flag=False -> store the link text.
_ATTR_KEYS: dict[str, tuple[str, bool]] = {
    "laundry": ("laundry", False),
    "parking": ("parking", False),
    "housing_type": ("housing_type", False),
    "rent_period": ("rent_period", False),
    "pets_cat": ("cats_ok", True),
    "pets_dog": ("dogs_ok", True),
    "is_furnished": ("furnished", True),
    "no_smoking": ("no_smoking", True),
    "wheelchaccess": ("wheelchair_accessible", True),
    "air_conditioning": ("air_conditioning", True),
    "ev_charging": ("ev_charging", True),
}

# Plausible monthly-rent bounds. Craigslist's "apartments for rent" section is
# polluted with teaser/spam posts priced at $1–$50 (a trick to sort to the top,
# with the real rent hidden in the body). Nothing legitimate in NYC falls below
# a few hundred a month or above six figures.
RENT_MIN = 300
RENT_MAX = 100_000


def plausible_rent(price: int | None) -> bool:
    """True if a price looks like a real NYC monthly rent."""
    return price is not None and RENT_MIN <= price <= RENT_MAX


_PID_RE = re.compile(r"/(\d+)\.html")
_ROW_RE = re.compile(r'<li class="cl-static-search-result".*?</li>', re.S)
_ATTR_LINK_RE = re.compile(
    r'<a href="[^"]*?/search/[a-z]{2,4}\?([a-z_]+)=[^"]*">(.*?)</a>', re.S
)
_BEDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*br", re.I)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ba\b", re.I)
_SQFT_RE = re.compile(r"([\d,]+)\s*ft(?:<sup>2</sup>|&sup2;|²|2)", re.I)
_NOFEE_RE = re.compile(r"no[\s\-]?fee", re.I)
# "Rent stabilized" is a prized NYC status (capped, renewable rent). Match the
# common phrasings — "rent stabilized"/"rent stabilization" and the looser
# "rent stable" — but require the words adjacent, so "the rent is stable" (a
# different, weaker claim) doesn't trip it.
_RENT_STABILIZED_RE = re.compile(r"rent[\s\-]?stab(?:il|le)", re.I)
# Photos: the slider caption reads "image 1 of N" (the authoritative total).
# Each photo also carries a `data-imgid="…"`, but it appears twice (main slide +
# thumbnail), so the fallback counts *distinct* ids. A post with no photos has
# neither (Craigslist shows a no_image.png placeholder), so the count is 0.
_IMG_TOTAL_RE = re.compile(r"image\s+\d+\s+of\s+(\d+)", re.I)
_IMGID_RE = re.compile(r'data-imgid="([^"]+)"')


class RentalSummary(BaseModel):
    """Cheap fields pulled from one search-results row."""

    pid: str
    url: str
    title: str | None = None
    price: int | None = None
    neighborhood: str | None = None

    @field_validator("pid", mode="before")
    @classmethod
    def _coerce_pid(cls, v: Any) -> str:
        return str(v).strip()


class RentalListing(BaseModel):
    """One normalized NYC rental listing (search + detail merged)."""

    pid: str
    source: str = "craigslist"
    url: str | None = None
    title: str | None = None
    neighborhood: str | None = None
    borough: str | None = None

    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    sqft: int | None = None
    housing_type: str | None = None

    laundry: str | None = None
    parking: str | None = None
    cats_ok: bool = False
    dogs_ok: bool = False
    furnished: bool = False
    no_smoking: bool = False
    wheelchair_accessible: bool = False
    air_conditioning: bool = False
    ev_charging: bool = False
    no_fee: bool = False
    # True when the title/body advertises a rent-stabilized unit (a capped,
    # renewable NYC rent). Surfaced with a green marker in the UI.
    rent_stabilized: bool = False
    # Authoritative rent-stabilized status from NY DHCR's BBL list (see
    # home_hunter.rentstab), resolved from a listing's street address at scrape
    # time. True/False when the address resolved; None = unknown (no street
    # address — e.g. Craigslist — or it didn't geocode). Distinct from the
    # text-claim above: this confirms it against the city record.
    rent_stabilized_confirmed: bool | None = None
    rent_period: str | None = None
    amenities: list[str] = []
    # Number of photos on the detail page (0 = none; None when no detail page
    # was fetched). A strong scam signal — real listings almost always have photos.
    image_count: int | None = None

    latitude: float | None = None
    longitude: float | None = None

    posted_at: datetime | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = {}


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).strip()
    return text or None


def _price_to_int(text: str | None) -> int | None:
    """First integer in ``text``, tolerant of cents and thousands separators.

    Handles both ``$3395.00`` (cents -> 3395) and ``$2.800`` / ``$2,800``
    (thousands -> 2800): a trailing separator followed by exactly two digits is
    treated as cents and dropped; any remaining ``,``/``.`` are separators.
    """
    if not text:
        return None
    m = re.search(r"\d[\d.,]*", text)
    if not m:
        return None
    num = re.sub(r"[.,]\d{2}$", "", m.group(0))   # drop trailing cents
    num = num.replace(",", "").replace(".", "")   # remaining = thousands seps
    return int(num) if num else None


def _first(pattern: re.Pattern[str], text: str, group: int = 1) -> str | None:
    m = pattern.search(text)
    return m.group(group) if m else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _pid_from_url(url: str) -> str | None:
    m = _PID_RE.search(url)
    return m.group(1) if m else None


def parse_search_results(page_html: str) -> list[RentalSummary]:
    """Extract the listing summaries embedded in a Craigslist search page."""
    summaries: dict[str, RentalSummary] = {}
    for row in _ROW_RE.findall(page_html):
        href = _first(re.compile(r'<a href="([^"]+)"'), row)
        if not href:
            continue
        pid = _pid_from_url(href)
        if pid is None or pid in summaries:
            continue
        summaries[pid] = RentalSummary(
            pid=pid,
            url=html.unescape(href),
            title=_clean(_first(re.compile(r'<div class="title">(.*?)</div>', re.S), row)),
            price=_price_to_int(
                _first(re.compile(r'<div class="price">(.*?)</div>', re.S), row)
            ),
            neighborhood=_clean(
                _first(re.compile(r'<div class="location">(.*?)</div>', re.S), row)
            ),
        )
    return list(summaries.values())


def _image_count(detail_html: str) -> int:
    """How many photos the detail page carries (0 if none)."""
    m = _IMG_TOTAL_RE.search(detail_html)
    if m:
        return int(m.group(1))
    return len(set(_IMGID_RE.findall(detail_html)))


def _parse_beds_baths(detail_html: str) -> tuple[float | None, float | None]:
    """Beds/baths from the `<span class="attr important">1BR / 1Ba</span>` block."""
    m = re.search(r'<span class="attr important">(.*?)</span>', detail_html, re.S)
    text = _clean(m.group(1)) if m else None
    if not text:
        return None, None
    beds: float | None = None
    if re.search(r"\bstudio\b", text, re.I):
        beds = 0.0
    else:
        b = _BEDS_RE.search(text)
        beds = float(b.group(1)) if b else None
    ba = _BATHS_RE.search(text)
    baths = float(ba.group(1)) if ba else None
    return beds, baths


def parse_detail(
    detail_html: str,
    summary: RentalSummary,
    *,
    borough: str | None = None,
) -> RentalListing:
    """Merge a search summary with structured fields from its detail page."""
    listing = RentalListing(
        pid=summary.pid,
        url=summary.url,
        title=summary.title,
        neighborhood=summary.neighborhood,
        borough=borough,
        price=summary.price,
    )

    title = _clean(_first(re.compile(r'<span id="titletextonly">(.*?)</span>', re.S), detail_html))
    if title:
        listing.title = title

    price = _price_to_int(_first(re.compile(r'<span class="price">(.*?)</span>', re.S), detail_html))
    if price is not None:
        listing.price = price

    listing.beds, listing.baths = _parse_beds_baths(detail_html)

    sqft = _first(_SQFT_RE, detail_html)
    if sqft:
        listing.sqft = _price_to_int(sqft)

    listing.image_count = _image_count(detail_html)

    # Structured attributes encoded as query-param links.
    amenities: list[str] = []
    for key, raw_text in _ATTR_LINK_RE.findall(detail_html):
        label = _clean(raw_text)
        if not label:
            continue
        mapping = _ATTR_KEYS.get(key)
        if mapping:
            field_name, is_flag = mapping
            setattr(listing, field_name, True if is_flag else label)
        amenities.append(label)
    listing.amenities = amenities

    # Lat/long from the map div.
    latlong = re.search(
        r'<div id="map"[^>]*data-latitude="([-\d.]+)"[^>]*data-longitude="([-\d.]+)"',
        detail_html,
    )
    if latlong:
        listing.latitude = float(latlong.group(1))
        listing.longitude = float(latlong.group(2))

    # Posted / updated timestamps.
    times = re.findall(r'<time[^>]*datetime="([^"]+)"', detail_html)
    if times:
        listing.posted_at = _parse_dt(times[0])
        listing.updated_at = _parse_dt(times[-1])

    # No-fee and rent-stabilized are NYC-specific free-text claims, not
    # structured Craigslist attrs; detect them in the title or body text.
    body = _first(re.compile(r'<section id="postingbody">(.*?)</section>', re.S), detail_html) or ""
    text_blob = (listing.title or "") + " " + body
    listing.no_fee = bool(_NOFEE_RE.search(text_blob))
    listing.rent_stabilized = bool(_RENT_STABILIZED_RE.search(text_blob))

    listing.raw = {
        "summary": summary.model_dump(),
        "attrs": amenities,
        "image_count": listing.image_count,
    }
    return listing
