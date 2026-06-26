"""Offline tests for the rent-stabilized confirmation feature.

Covers the pure BBL lookup (``home_hunter.rentstab``) against the bundled DHCR
set, and the enrichment layer (``home_hunter.rentstab.geocode``) with the
network geocoder monkeypatched — so no test hits GeoSearch.
"""

from home_hunter import rentstab
from home_hunter.rentstab import geocode
from home_hunter.scraper.craigslist.parse import RentalListing

# A BBL present in the bundled set (first data line of stabilized_bbls.txt).
STABILIZED_BBL = "1000077501"


def test_normalize_bbl():
    assert rentstab.normalize_bbl("1000077501") == "1000077501"
    assert rentstab.normalize_bbl(1000077501) == "1000077501"
    assert rentstab.normalize_bbl("1-00007-7501") == "1000077501"  # strips punctuation
    assert rentstab.normalize_bbl("999") is None  # too short
    assert rentstab.normalize_bbl(None) is None


def test_bundled_set_loaded():
    # The committed bundle is non-trivial (tens of thousands of BBLs).
    assert rentstab.stabilized_count() > 40000


def test_is_stabilized():
    assert rentstab.is_stabilized(STABILIZED_BBL) is True
    assert rentstab.is_stabilized(int(STABILIZED_BBL)) is True
    assert rentstab.is_stabilized("9999999999") is False  # valid shape, not listed
    assert rentstab.is_stabilized(None) is False


def _renthop(pid: str, title: str | None) -> RentalListing:
    return RentalListing(pid=pid, source="renthop", title=title, borough="Manhattan")


def test_confirmed_status_resolved_stabilized(monkeypatch):
    monkeypatch.setattr(geocode, "bbl_for", lambda address: STABILIZED_BBL)
    assert geocode.confirmed_status(_renthop("rh-1", "100 Real St")) is True


def test_confirmed_status_resolved_not_stabilized(monkeypatch):
    monkeypatch.setattr(geocode, "bbl_for", lambda address: "9999999999")
    assert geocode.confirmed_status(_renthop("rh-2", "200 Real St")) is False


def test_confirmed_status_unresolved_is_none(monkeypatch):
    monkeypatch.setattr(geocode, "bbl_for", lambda address: None)
    assert geocode.confirmed_status(_renthop("rh-3", "Nowhere Ave")) is None


def test_craigslist_source_is_never_geocoded(monkeypatch):
    calls = []

    def spy(address):
        calls.append(address)
        return STABILIZED_BBL

    monkeypatch.setattr(geocode, "bbl_for", spy)
    cl = RentalListing(pid="123", source="craigslist", title="Sunny 2BR in Kips Bay")
    assert geocode.confirmed_status(cl) is None
    assert calls == []  # address-less sources are skipped entirely


def test_enrich_listings_sets_field_and_skips_non_address_sources(monkeypatch):
    monkeypatch.setattr(geocode, "bbl_for", lambda address: STABILIZED_BBL)
    rh = _renthop("rh-9", "300 Real St")
    cl = RentalListing(pid="456", source="craigslist", title="cozy studio")
    listings = [rh, cl]
    out = geocode.enrich_listings(listings)
    assert out is listings  # mutates and returns the same list
    assert rh.rent_stabilized_confirmed is True
    assert cl.rent_stabilized_confirmed is None  # untouched
