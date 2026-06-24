"""Per-neighborhood rent statistics.

A pure, offline-testable aggregator (no I/O, no DB) following the same template
as ``flags.py`` / ``dedup.py``. The API layer pulls the relevant columns out of
the database and hands them here as plain ``RentalStatRow`` records; this module
groups them by neighborhood, buckets by bed count (studio / 1 / 2 / 3+), and
computes the rent stats the Analytics tab renders.

Keeping it pure means the whole table can be asserted against hand-built rows in
a fast offline test, with no engine or fixtures.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Iterable, NamedTuple

# Bed-count buckets, in display order. ``None`` beds count toward a
# neighborhood's total but land in no bucket.
BED_BUCKETS = ("studio", "1", "2", "3+")


class RentalStatRow(NamedTuple):
    """One listing's fields needed for neighborhood stats (DB-agnostic)."""

    neighborhood: str
    borough: str | None
    beds: float | None
    price: int | None
    sqft: int | None
    no_fee: bool


def _bucket_for(beds: float | None) -> str | None:
    """Map a bed count to its bucket label, or ``None`` to skip bucketing."""
    if beds is None:
        return None
    b = int(beds)  # floor fractional counts (Craigslist beds are ~integers)
    if b <= 0:
        return "studio"
    if b == 1:
        return "1"
    if b == 2:
        return "2"
    return "3+"


def _bucket_stats(prices: list[int]) -> dict:
    """count / avg / median / min / max for one bed bucket's rents."""
    return {
        "count": len(prices),
        "avg": round(statistics.mean(prices)),
        "median": round(statistics.median(prices)),
        "min": min(prices),
        "max": max(prices),
    }


def neighborhood_rent_stats(
    rows: Iterable[RentalStatRow], *, min_listings: int = 1
) -> list[dict]:
    """Aggregate listing rows into per-neighborhood rent statistics.

    Only rows with a ``neighborhood`` and a non-``None`` ``price`` contribute.
    For each neighborhood the result carries its ``total`` listing count, the
    most common ``borough``, an average ``ppsf`` (price per ft², over rows with
    ``sqft > 0``; ``None`` when none have sqft), ``no_fee_pct`` (rounded int),
    and a ``beds`` map of bucket -> {count, avg, median, min, max} for whichever
    of studio/1/2/3+ have listings.

    Neighborhoods with fewer than ``min_listings`` priced listings are dropped.
    Results are sorted by ``total`` descending (most data first).
    """
    # neighborhood -> bucket -> [prices]
    bucket_prices: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    boroughs: dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    ppsf_values: dict[str, list[float]] = defaultdict(list)
    no_fee_counts: Counter = Counter()

    for row in rows:
        if not row.neighborhood or row.price is None:
            continue
        nb = row.neighborhood
        totals[nb] += 1
        if row.borough:
            boroughs[nb][row.borough] += 1
        if row.no_fee:
            no_fee_counts[nb] += 1
        if row.sqft and row.sqft > 0:
            ppsf_values[nb].append(row.price / row.sqft)
        bucket = _bucket_for(row.beds)
        if bucket is not None:
            bucket_prices[nb][bucket].append(row.price)

    results: list[dict] = []
    for nb, total in totals.items():
        if total < min_listings:
            continue
        beds = {
            bucket: _bucket_stats(bucket_prices[nb][bucket])
            for bucket in BED_BUCKETS
            if bucket_prices[nb][bucket]
        }
        ppsf_list = ppsf_values[nb]
        results.append(
            {
                "neighborhood": nb,
                "borough": boroughs[nb].most_common(1)[0][0] if boroughs[nb] else None,
                "total": total,
                "ppsf": round(statistics.mean(ppsf_list), 1) if ppsf_list else None,
                "no_fee_pct": round(100 * no_fee_counts[nb] / total),
                "beds": beds,
            }
        )

    results.sort(key=lambda r: r["total"], reverse=True)
    return results
