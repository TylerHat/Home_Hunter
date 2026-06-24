"""Pure, offline tests for per-neighborhood rent aggregation (analytics.py)."""

from home_hunter.analytics import RentalStatRow, neighborhood_rent_stats


def _row(neighborhood="Astoria", borough="Queens", beds=1, price=3000, sqft=None, no_fee=False):
    return RentalStatRow(neighborhood, borough, beds, price, sqft, no_fee)


def test_buckets_with_avg_median_min_max():
    rows = [
        _row(beds=0, price=2000),
        _row(beds=0, price=2200),
        _row(beds=1, price=3000),
        _row(beds=1, price=3200),
        _row(beds=1, price=3400),   # 1BR avg & median = 3200
        _row(beds=2, price=4000),
        _row(beds=3, price=5000),   # 3+ bucket
    ]
    [nb] = neighborhood_rent_stats(rows)
    assert nb["neighborhood"] == "Astoria"
    assert nb["total"] == 7
    assert nb["beds"]["studio"] == {"count": 2, "avg": 2100, "median": 2100, "min": 2000, "max": 2200}
    one = nb["beds"]["1"]
    assert (one["count"], one["avg"], one["median"], one["min"], one["max"]) == (3, 3200, 3200, 3000, 3400)
    assert nb["beds"]["2"]["count"] == 1
    assert nb["beds"]["3+"]["count"] == 1


def test_even_count_median_is_midpoint():
    [nb] = neighborhood_rent_stats([_row(beds=1, price=3000), _row(beds=1, price=3100)])
    assert nb["beds"]["1"]["median"] == 3050
    assert nb["beds"]["1"]["avg"] == 3050


def test_ppsf_only_counts_rows_with_positive_sqft():
    rows = [
        _row(price=3000, sqft=600),    # 5.0 $/ft²
        _row(price=4000, sqft=800),    # 5.0 $/ft²
        _row(price=3500, sqft=None),   # ignored
        _row(price=3500, sqft=0),      # ignored (no divide-by-zero)
    ]
    [nb] = neighborhood_rent_stats(rows)
    assert nb["ppsf"] == 5.0


def test_ppsf_none_when_no_sqft():
    [nb] = neighborhood_rent_stats([_row(price=3000)])
    assert nb["ppsf"] is None


def test_no_fee_pct_rounds():
    rows = [_row(no_fee=True), _row(no_fee=False), _row(no_fee=False)]
    [nb] = neighborhood_rent_stats(rows)
    assert nb["no_fee_pct"] == 33   # 1 of 3


def test_min_listings_drops_small_neighborhoods():
    rows = [_row(neighborhood="Big") for _ in range(4)] + [_row(neighborhood="Small")]
    out = neighborhood_rent_stats(rows, min_listings=2)
    assert [n["neighborhood"] for n in out] == ["Big"]


def test_results_sorted_by_total_desc():
    rows = (
        [_row(neighborhood="A") for _ in range(2)]
        + [_row(neighborhood="B") for _ in range(5)]
    )
    out = neighborhood_rent_stats(rows)
    assert [n["neighborhood"] for n in out] == ["B", "A"]


def test_none_beds_count_toward_total_but_no_bucket():
    [nb] = neighborhood_rent_stats([_row(beds=None, price=3000), _row(beds=1, price=3200)])
    assert nb["total"] == 2
    assert set(nb["beds"]) == {"1"}


def test_rows_without_neighborhood_or_price_are_skipped():
    rows = [
        _row(neighborhood="", price=3000),
        _row(neighborhood="Astoria", price=None),
        _row(neighborhood="Astoria", price=3000),
    ]
    out = neighborhood_rent_stats(rows)
    assert len(out) == 1 and out[0]["total"] == 1


def test_most_common_borough_wins():
    rows = [
        _row(neighborhood="Greenpoint", borough="Brooklyn"),
        _row(neighborhood="Greenpoint", borough="Brooklyn"),
        _row(neighborhood="Greenpoint", borough="Queens"),   # mislabeled outlier
    ]
    [nb] = neighborhood_rent_stats(rows)
    assert nb["borough"] == "Brooklyn"
