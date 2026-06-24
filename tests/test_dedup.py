"""Tests for the repost fingerprint (pure, offline)."""

from home_hunter import dedup


def _fp(title, price=3000, beds=1.0, borough="Queens"):
    return dedup.fingerprint(title=title, price=price, beds=beds, borough=borough)


def test_reposts_share_a_fingerprint_despite_cosmetic_title_edits():
    # Same apartment re-posted: casing, punctuation, and emoji differ only.
    a = _fp("Sunny 1BR — Exposed Brick!")
    b = _fp("sunny 1br  exposed brick")
    c = _fp("✨Sunny 1BR, Exposed Brick✨")
    assert a == b == c


def test_distinct_units_at_same_address_get_distinct_fingerprints():
    # The over-merge case coords would wrongly collapse: different floor-plans.
    a = _fp("Spacious 3 Bedroom, 1.5 Bath Split-Level", beds=3.0)
    b = _fp("Spacious 3 Bedroom, 2.5 Bath Split-Level", beds=3.0)
    assert a != b


def test_price_and_beds_and_borough_distinguish_listings():
    base = _fp("No Fee 2BR")
    assert base != _fp("No Fee 2BR", price=3200)
    assert base != _fp("No Fee 2BR", beds=2.0)
    assert base != _fp("No Fee 2BR", borough="Brooklyn")


def test_thin_listings_are_not_fingerprinted():
    # Without a title or a price there isn't enough signal to dedupe safely.
    assert dedup.fingerprint(title=None, price=3000, beds=1.0) is None
    assert dedup.fingerprint(title="   ", price=3000, beds=1.0) is None
    assert dedup.fingerprint(title="1BR", price=None, beds=1.0) is None


def test_missing_beds_still_fingerprints():
    # Summary-only listings (no detail fetch) have no beds but still dedupe.
    assert _fp("Charming Studio", beds=None) is not None
