"""Offline tests for Craigslist parsing — no network required."""

from pathlib import Path

import pytest

from home_hunter.scraper.craigslist.parse import (
    RentalSummary,
    parse_detail,
    parse_search_results,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "craigslist_search.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "craigslist_detail.html").read_text(encoding="utf-8")

# The first search row and the detail page are the same listing.
LISTING_PID = "7942602776"


def test_search_results_parsed():
    summaries = parse_search_results(SEARCH_HTML)
    assert len(summaries) == 6
    first = summaries[0]
    assert first.pid == LISTING_PID
    assert first.url.endswith(f"{LISTING_PID}.html")
    assert first.price == 3850
    assert first.neighborhood == "Upper West Side"
    assert "1BR" in (first.title or "")


def test_search_pids_unique():
    summaries = parse_search_results(SEARCH_HTML)
    pids = [s.pid for s in summaries]
    assert len(pids) == len(set(pids))


@pytest.fixture
def detail():
    summary = RentalSummary(
        pid=LISTING_PID,
        url=f"https://newyork.craigslist.org/mnh/apa/d/x/{LISTING_PID}.html",
        price=3850,
        neighborhood="Upper West Side",
    )
    return parse_detail(DETAIL_HTML, summary, borough="Manhattan")


def test_detail_core_fields(detail):
    assert detail.pid == LISTING_PID
    assert detail.price == 3850
    assert detail.beds == 1.0
    assert detail.baths == 1.0
    assert detail.borough == "Manhattan"
    assert detail.latitude == pytest.approx(40.774282)
    assert detail.longitude == pytest.approx(-73.979294)
    assert detail.posted_at is not None


def test_detail_amenities(detail):
    assert detail.cats_ok is True
    assert detail.dogs_ok is True
    assert detail.laundry == "laundry in bldg"
    assert detail.parking == "street parking"
    assert detail.housing_type == "apartment"
    assert detail.rent_period == "monthly"
    # Catch-all keeps the raw labels too.
    assert "cats are OK - purrr" in detail.amenities


def test_detail_no_fee_detected(detail):
    # Title contains "NO FEE".
    assert detail.no_fee is True


def test_detail_absent_amenities_default_false(detail):
    # This listing advertises no EV charging / wheelchair access.
    assert detail.ev_charging is False
    assert detail.wheelchair_accessible is False


def test_sqft_and_studio_parsing():
    html = (
        '<span id="titletextonly">Studio</span>'
        '<span class="price">$2,500</span>'
        '<span class="housing"> / studio - 1,200ft<sup>2</sup> - </span>'
        '<span class="attr important">studio</span>'
        '<div id="map" data-latitude="40.7" data-longitude="-73.9" data-accuracy="5"></div>'
    )
    summary = RentalSummary(pid="9", url="https://newyork.craigslist.org/x/9.html")
    r = parse_detail(html, summary)
    assert r.sqft == 1200
    assert r.beds == 0.0   # studio -> 0 bedrooms
    assert r.price == 2500
