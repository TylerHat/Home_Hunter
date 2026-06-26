"""Offline tests for the GET /rentals filters (source + rent-stabilized).

Forces an in-memory SQLite engine before importing the app (so the module-level
engine never touches a real/Neon DB), then overrides the request session with a
seeded in-memory one. Stays fully offline.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from home_hunter.db import upsert_listing
from home_hunter.models import Base
from home_hunter.scraper.craigslist.parse import RentalListing

# Williamsburg, Brooklyn — resolves via home_hunter.geo (see tests/test_db.py).
WBURG = {"borough": "Brooklyn", "latitude": 40.7081, "longitude": -73.9571}


@pytest.fixture
def client():
    os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    from fastapi.testclient import TestClient

    from home_hunter.api.app import app, get_session  # import after env is forced

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        # Craigslist, not rent-stabilized.
        upsert_listing(s, RentalListing(pid="cl1", source="craigslist",
                                        title="Sunny one bedroom", price=3000,
                                        beds=1, image_count=5, **WBURG))
        # RentHop, advertised rent-stabilized (text claim only).
        upsert_listing(s, RentalListing(pid="rh-1", source="renthop",
                                        title="Rent stabilized gem", price=2400,
                                        beds=1, image_count=4,
                                        rent_stabilized=True, **WBURG))
        # RentHop, DHCR-confirmed rent-stabilized (no text claim).
        upsert_listing(s, RentalListing(pid="rh-2", source="renthop",
                                        title="Quiet studio", price=2100, beds=0,
                                        image_count=3,
                                        rent_stabilized_confirmed=True, **WBURG))
        # RentHop, not rent-stabilized.
        upsert_listing(s, RentalListing(pid="rh-3", source="renthop",
                                        title="Market-rate two bed", price=4200,
                                        beds=2, image_count=6, **WBURG))
        s.commit()

    def override_session():
        with Session() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _pids(resp):
    return {r["pid"] for r in resp.json()}


def test_no_source_filter_returns_all(client):
    assert _pids(client.get("/rentals")) == {"cl1", "rh-1", "rh-2", "rh-3"}


def test_filter_by_craigslist_source(client):
    assert _pids(client.get("/rentals?source=craigslist")) == {"cl1"}


def test_filter_by_renthop_source(client):
    assert _pids(client.get("/rentals?source=renthop")) == {"rh-1", "rh-2", "rh-3"}


def test_rent_stabilized_matches_advertised_and_confirmed(client):
    # Advertised (rh-1) OR DHCR-confirmed (rh-2), like the green UI badge.
    assert _pids(client.get("/rentals?rent_stabilized=true")) == {"rh-1", "rh-2"}


def test_rent_stabilized_off_returns_all(client):
    assert _pids(client.get("/rentals?rent_stabilized=false")) == {
        "cl1", "rh-1", "rh-2", "rh-3"
    }


def test_source_and_rent_stabilized_combine(client):
    resp = client.get("/rentals?source=renthop&rent_stabilized=true")
    assert _pids(resp) == {"rh-1", "rh-2"}
