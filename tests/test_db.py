"""Tests for upsert + rent-history logic using an in-memory SQLite DB."""

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from home_hunter.db import _ensure_columns, recompute_market_flags, upsert_listing
from home_hunter.models import Base, Rental, RentHistory
from home_hunter.scraper.craigslist.parse import RentalListing


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        yield s


def _listing(price: int) -> RentalListing:
    return RentalListing(
        pid="111", title="1BR in Astoria", neighborhood="Astoria",
        borough="Queens", price=price, beds=1, baths=1, cats_ok=True,
    )


def test_insert_creates_rental_and_rent_row(session):
    stats = upsert_listing(session, _listing(3000))
    session.commit()
    assert stats.inserted == 1 and stats.price_changes == 1
    row = session.get(Rental, "111")
    assert row.price == 3000
    assert row.cats_ok is True
    assert len(session.scalars(select(RentHistory)).all()) == 1


def test_reupsert_same_rent_adds_no_history(session):
    upsert_listing(session, _listing(3000))
    session.commit()
    stats = upsert_listing(session, _listing(3000))
    session.commit()
    assert stats.updated == 1 and stats.price_changes == 0
    assert len(session.scalars(select(RentHistory)).all()) == 1


def test_rent_change_appends_history(session):
    upsert_listing(session, _listing(3000))
    session.commit()
    stats = upsert_listing(session, _listing(2850))  # rent drop
    session.commit()
    assert stats.price_changes == 1
    row = session.get(Rental, "111")
    assert row.price == 2850
    history = session.scalars(
        select(RentHistory).order_by(RentHistory.observed_at)
    ).all()
    assert [h.price for h in history] == [3000, 2850]


def _repost(pid: str, title: str = "1BR in Astoria", price: int = 3000) -> RentalListing:
    """A repost of ``_listing`` under a new pid (same fingerprint fields)."""
    return RentalListing(
        pid=pid, title=title, neighborhood="Astoria", borough="Queens",
        price=price, beds=1, baths=1, cats_ok=True,
    )


def test_repost_under_new_pid_updates_existing_row(session):
    upsert_listing(session, _listing(3000))  # pid 111
    session.commit()
    stats = upsert_listing(session, _repost("999"))  # same apartment, new pid
    session.commit()
    assert stats.duplicates_merged == 1 and stats.inserted == 0
    # Only the original row survives; the repost did not create a second row.
    assert len(session.scalars(select(Rental)).all()) == 1
    assert session.get(Rental, "999") is None
    assert session.get(Rental, "111") is not None


def test_repost_carries_over_newest_url(session):
    upsert_listing(session, _listing(3000))
    session.commit()
    fresh = _repost("999")
    fresh.url = "https://example.com/que/apa/d/astoria/999.html"
    upsert_listing(session, fresh)
    session.commit()
    assert session.get(Rental, "111").url == fresh.url


def test_distinct_titles_are_not_merged(session):
    upsert_listing(session, _listing(3000))
    session.commit()
    stats = upsert_listing(session, _repost("999", title="Different 1BR in Astoria"))
    session.commit()
    assert stats.inserted == 1 and stats.duplicates_merged == 0
    assert len(session.scalars(select(Rental)).all()) == 2


def test_upsert_tags_neighborhood_key_from_coordinates(session):
    # Coordinates in Williamsburg, Brooklyn — resolved via home_hunter.geo.
    listing = RentalListing(
        pid="222", title="Loft", borough="Brooklyn", price=3500,
        latitude=40.7081, longitude=-73.9571,
    )
    upsert_listing(session, listing)
    session.commit()
    assert session.get(Rental, "222").neighborhood_key == "Williamsburg"


def test_upsert_leaves_neighborhood_key_none_without_coordinates(session):
    upsert_listing(session, _listing(3000))  # no lat/long
    session.commit()
    assert session.get(Rental, "111").neighborhood_key is None


def test_upsert_flags_listing_without_photos(session):
    listing = RentalListing(
        pid="333", title="Bright 1BR in Astoria", borough="Queens",
        price=3000, beds=1, image_count=0, latitude=40.7, longitude=-73.9,
    )
    upsert_listing(session, listing)
    session.commit()
    row = session.get(Rental, "333")
    assert row.flagged is True
    assert "no photos" in row.flag_reasons


def test_upsert_does_not_flag_listing_with_photos(session):
    listing = RentalListing(
        pid="444", title="Bright 1BR in Astoria", borough="Queens",
        price=3000, beds=1, image_count=6, latitude=40.7, longitude=-73.9,
    )
    upsert_listing(session, listing)
    session.commit()
    assert session.get(Rental, "444").flagged is False


def test_recompute_market_flags_adds_below_median_signal(session):
    # A full cohort of normal 1BRs plus one suspiciously cheap, photoless listing.
    for i, price in enumerate([4000, 4100, 3900, 4200, 4050]):
        upsert_listing(session, RentalListing(
            pid=f"n{i}", title=f"1BR number {i}", borough="Queens",
            price=price, beds=1, image_count=5, latitude=40.7, longitude=-73.9,
        ))
    upsert_listing(session, RentalListing(
        pid="cheap", title="1BR steal in Astoria", borough="Queens",
        price=1200, beds=1, image_count=0, latitude=40.7, longitude=-73.9,
    ))
    session.commit()

    flagged = recompute_market_flags(session)
    session.commit()

    cheap = session.get(Rental, "cheap")
    assert flagged == 1
    assert cheap.flagged is True
    assert {"no photos", "rent far below area median"} <= set(cheap.flag_reasons)
    # The normal, photo-bearing listings stay clean.
    assert session.get(Rental, "n0").flagged is False


def test_ensure_columns_adds_missing_columns():
    # Simulate a DB created before the columns existed: build a bare table.
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE rentals (pid VARCHAR PRIMARY KEY, price INTEGER)"))
    _ensure_columns(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("rentals")}
    assert {"neighborhood_key", "dedup_key", "image_count", "flagged", "flag_reasons"} <= cols
    _ensure_columns(engine)  # idempotent — a second run must not error
