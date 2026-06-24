"""Heuristic scam / fake-listing detection.

A pure, offline-testable scorer (no I/O, no DB) mirroring the planned
``scoring.py`` design. ``evaluate()`` weighs a handful of signals into a risk
score and a list of human-readable reasons; a listing is *flagged* when the
score crosses ``threshold``.

The dominant signal is **missing photos**: a genuine NYC rental almost always
has pictures, while teaser/scam posts frequently have none. Defaults are tuned
so an absent photo set flags a listing on its own, while weaker signals only
flag in combination — heuristics produce false positives, so the pipeline keeps
flagged listings (badged) rather than dropping them.

``evaluate()`` is duck-typed: it accepts anything exposing ``image_count``,
``price``, ``beds``, ``latitude``, ``longitude``, ``rent_period`` and ``title``
— both the scraped ``RentalListing`` (at upsert) and the ORM ``Rental`` (in the
market recompute pass).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class FlagSettings:
    """Tunable weights/thresholds, loaded from the ``flags:`` block of config."""

    enabled: bool = True
    threshold: int = 100           # risk at/above this -> flagged
    no_photo_weight: int = 100     # dominant: alone trips the threshold
    implausible_rent_weight: int = 100
    missing_location_weight: int = 40
    below_median_weight: int = 60
    minor_weight: int = 20         # weekly rent / very short title
    market_ratio: float = 0.5      # flag if rent < this fraction of cohort median
    min_cohort_size: int = 5       # don't trust a median from a tiny cohort


@dataclass(frozen=True)
class FlagResult:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    risk: int = 0


class _ListingLike(Protocol):
    image_count: int | None
    price: int | None
    beds: float | None
    latitude: float | None
    longitude: float | None
    rent_period: str | None
    title: str | None


def _is_plausible_rent(price: int | None) -> bool:
    # Lazy import keeps this module free of package-level imports (the scraper
    # package imports config, so a top-level import here would risk a cycle).
    from .scraper.craigslist.parse import plausible_rent

    return plausible_rent(price)


def evaluate(
    listing: _ListingLike,
    *,
    settings: FlagSettings | None = None,
    market_median: float | None = None,
) -> FlagResult:
    """Score one listing for scam risk.

    ``market_median`` is the median rent for the listing's cohort (e.g. same
    borough + bed count). Pass it only when the cohort is large enough to trust
    (see ``min_cohort_size``); omit it to skip the market-relative signal — for
    instance at upsert time, before the full dataset is known.
    """
    settings = settings or FlagSettings()
    if not settings.enabled:
        return FlagResult(flagged=False)

    reasons: list[str] = []
    risk = 0

    # Primary signal: no photos. Only a *known* zero counts — None means no
    # detail page was fetched, so we don't penalize the unknown.
    if getattr(listing, "image_count", None) == 0:
        reasons.append("no photos")
        risk += settings.no_photo_weight

    price = getattr(listing, "price", None)
    if price is not None and not _is_plausible_rent(price):
        reasons.append("implausible rent")
        risk += settings.implausible_rent_weight

    if getattr(listing, "latitude", None) is None or getattr(listing, "longitude", None) is None:
        reasons.append("no map location")
        risk += settings.missing_location_weight

    if market_median and price is not None and price < settings.market_ratio * market_median:
        reasons.append("rent far below area median")
        risk += settings.below_median_weight

    rent_period = getattr(listing, "rent_period", None)
    if rent_period and "week" in rent_period.lower():
        reasons.append("weekly rental")
        risk += settings.minor_weight

    title = getattr(listing, "title", None)
    if title is not None and len(title.split()) < 3:
        reasons.append("very short title")
        risk += settings.minor_weight

    return FlagResult(flagged=risk >= settings.threshold, reasons=reasons, risk=risk)
