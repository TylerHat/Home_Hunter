"""Resolve a listing's street address to a BBL and confirm rent-stabilization.

This is the **network** half of the rent-stab enrichment: NYC GeoSearch (a free,
key-less NYC Planning Labs service) turns a free-text street address into a BBL,
which the bundled set in :mod:`home_hunter.rentstab` then confirms. The pure
lookup has no I/O; this module does, so it lives apart and is used at scrape time
only.

Design notes:
* Only sources whose ``title`` is a real street address are geocoded (RentHop);
  Craigslist's free-text titles aren't addresses, so those listings keep the
  text-only ``rent_stabilized`` signal and a ``None`` (unknown) confirmation.
* Failures degrade to ``None`` — a flaky geocoder never aborts a run.
* Results are cached per address and the geocoder is paced politely.
"""

from __future__ import annotations

import logging
import random
import time
from functools import lru_cache

from . import is_stabilized, normalize_bbl

logger = logging.getLogger(__name__)

GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"

# Sources whose listing.title is a real street address worth geocoding.
ADDRESS_SOURCES = frozenset({"renthop"})

# Use the OS trust store so a corporate TLS-inspection proxy doesn't break HTTPS
# (same rationale as the Craigslist client). No-op without ``truststore``.
try:
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception:  # pragma: no cover - optional dependency
    pass

try:
    import httpx
except Exception:  # pragma: no cover - httpx is a core dep; guarded for safety
    httpx = None  # type: ignore

_client: "httpx.Client | None" = None


def _http() -> "httpx.Client | None":
    global _client
    if _client is None and httpx is not None:
        _client = httpx.Client(
            timeout=10.0, headers={"User-Agent": "home_hunter/rentstab"}
        )
    return _client


@lru_cache(maxsize=8192)
def bbl_for(address: str) -> str | None:
    """The BBL for a street address via GeoSearch, or ``None`` if unresolved.

    Cached per address. A paced, best-effort network call: any error (network,
    non-200, empty/odd JSON) returns ``None`` rather than raising.
    """
    client = _http()
    if not address or client is None:
        return None
    time.sleep(random.uniform(0.05, 0.15))  # be polite to the free geocoder
    try:
        resp = client.get(GEOSEARCH_URL, params={"text": address, "size": 1})
        if resp.status_code != 200:
            return None
        features = resp.json().get("features") or []
        if not features:
            return None
        props = features[0].get("properties") or {}
        # GeoSearch puts the BBL under addendum.pad.bbl; pad_bbl is a fallback.
        pad = (props.get("addendum") or {}).get("pad") or {}
        return normalize_bbl(pad.get("bbl") or props.get("pad_bbl"))
    except Exception as exc:  # network / JSON error -> unknown, never fatal
        logger.debug("geosearch failed for %r: %s", address, exc)
        return None


def _address_text(listing) -> str | None:
    """Build a geocodable address string from a listing's title + borough."""
    title = (getattr(listing, "title", None) or "").strip()
    if not title:
        return None
    borough = (getattr(listing, "borough", None) or "").strip()
    return f"{title}, {borough}, NY" if borough else f"{title}, NY"


def confirmed_status(listing) -> bool | None:
    """``True``/``False`` rent-stabilized for a resolvable listing, else ``None``.

    ``None`` means *unknown*: the source has no street address, the address
    didn't resolve, or the geocoder was unavailable — distinct from a confirmed
    ``False`` (resolved to a building with no DHCR-stabilized units).
    """
    if getattr(listing, "source", None) not in ADDRESS_SOURCES:
        return None
    address = _address_text(listing)
    if not address:
        return None
    bbl = bbl_for(address)
    if bbl is None:
        return None
    return is_stabilized(bbl)


def enrich_listings(listings: list) -> list:
    """Set ``rent_stabilized_confirmed`` on each address-bearing listing.

    Mutates and returns ``listings``. Listings without a resolvable street
    address are left at ``None`` (unknown). Counts confirmed hits for the log.
    """
    confirmed = 0
    for listing in listings:
        status = confirmed_status(listing)
        if status is not None:
            listing.rent_stabilized_confirmed = status
            confirmed += int(status)
    if confirmed:
        logger.info("rent-stab: confirmed %d listing(s) from DHCR BBLs", confirmed)
    return listings
