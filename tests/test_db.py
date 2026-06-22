"""Tests for upsert + rent-history logic using an in-memory SQLite DB."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from home_hunter.db import upsert_listing
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
