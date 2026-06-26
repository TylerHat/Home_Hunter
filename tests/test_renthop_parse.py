"""Offline tests for the RentHop search-page parser.

Drives ``parse_search_results`` against a trimmed real RentHop results page
(``fixtures/renthop_search.html``) plus a couple of synthetic cards for the
studio and borough-fallback paths. Never touches the network.
"""

from pathlib import Path

from home_hunter.scraper.renthop.parse import parse_search_results

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "renthop_search.html").read_text(encoding="utf-8")


def test_parses_all_cards_with_shared_model():
    rows = parse_search_results(SEARCH_HTML, borough="Manhattan")
    assert len(rows) == 4
    # Every row is a shared RentalListing tagged as renthop with an rh- pid.
    assert all(r.source == "renthop" for r in rows)
    assert all(r.pid.startswith("rh-") for r in rows)
    # No detail fetch needed: the card already carries the core fields.
    assert all(r.price is not None for r in rows)
    assert all(r.beds is not None for r in rows)
    assert all(r.latitude is not None and r.longitude is not None for r in rows)


def test_first_card_fields():
    first = parse_search_results(SEARCH_HTML, borough="Manhattan")[0]
    assert first.pid == "rh-76540439"
    assert first.price == 6789
    assert first.beds == 2.0 and first.baths == 1.0
    assert first.sqft == 762
    assert first.no_fee is True
    assert first.borough == "Manhattan"
    assert first.neighborhood == "Kips Bay"
    assert first.latitude == 40.7403 and first.longitude == -73.9786
    assert first.url == "https://www.renthop.com/listings/2nd-avenue/22c/76540439"


def test_full_street_address_preserved():
    # RentHop carries real addresses (with house numbers) — the basis for the
    # rent-stabilized BBL confirmation. A street-only title also parses.
    by_pid = {r.pid: r for r in parse_search_results(SEARCH_HTML, borough="Manhattan")}
    assert by_pid["rh-76736561"].title == "703 9th Avenue, Apt 4C"
    assert by_pid["rh-76731783"].title == "East 14th Street"  # street-only, no number


def test_sqft_optional():
    by_pid = {r.pid: r for r in parse_search_results(SEARCH_HTML, borough="Manhattan")}
    assert by_pid["rh-76736561"].sqft is None  # card without a Sqft figure
    assert by_pid["rh-75458237"].sqft == 1300


def test_studio_card_is_zero_beds():
    card = (
        "<div class=\"search-listing\" id=\"listing-555\" listing_id='555' "
        "latitude='40.70' longitude='-73.95'>"
        "<div id=\"listing-555-title\">100 Test St, Apt 1</div>"
        "<div id='listing-555-neighborhoods'>Dumbo, Brooklyn</div>"
        "<div id=\"listing-555-price\">$2,500</div> Studio | 1 Bath</div>"
    )
    rows = parse_search_results(card, borough="Manhattan")
    assert len(rows) == 1
    assert rows[0].beds == 0.0
    # Borough comes from the card's own neighborhoods line, not the passed-in area.
    assert rows[0].borough == "Brooklyn"
    assert rows[0].neighborhood == "Dumbo"


def test_borough_falls_back_to_area_when_card_lacks_neighborhoods():
    card = (
        "<div class=\"search-listing\" id=\"listing-7\" listing_id='7' "
        "latitude='40.6' longitude='-74.0'>"
        "<div id=\"listing-7-price\">$1,900</div> 1 Bed | 1 Bath</div>"
    )
    rows = parse_search_results(card, borough="Staten Island")
    assert rows[0].borough == "Staten Island"
    assert rows[0].neighborhood is None
