"""Offline smoke test for the /analytics/neighborhoods endpoint.

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
    from home_hunter.api.app import app, get_session  # import after env is forced
    from fastapi.testclient import TestClient

    # StaticPool + a single shared connection so the in-memory DB is visible from
    # the TestClient's request thread (the default pool would hand that thread a
    # fresh, empty database).
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as s:
        upsert_listing(s, RentalListing(pid="a1", title="Sunny one bedroom loft",
                                        price=3000, beds=1, image_count=5, **WBURG))
        upsert_listing(s, RentalListing(pid="a2", title="Renovated one bedroom apartment",
                                        price=3200, beds=1, image_count=4, **WBURG))
        upsert_listing(s, RentalListing(pid="a3", title="Cozy studio near the park",
                                        price=2200, beds=0, image_count=3, **WBURG))
        # No photos -> flagged as a suspected scam -> excluded from stats by default.
        upsert_listing(s, RentalListing(pid="scam", title="Cheap one bedroom steal must go",
                                        price=900, beds=1, image_count=0, **WBURG))
        s.commit()

    def override_session():
        with Session() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_excludes_flagged_by_default(client):
    data = client.get("/analytics/neighborhoods?min_listings=1").json()
    assert data["excluded_flagged"] is True
    wb = next(n for n in data["neighborhoods"] if n["neighborhood"] == "Williamsburg")
    assert wb["total"] == 3                       # the flagged listing is left out
    assert wb["beds"]["1"]["avg"] == 3100         # (3000 + 3200) / 2
    assert wb["beds"]["studio"]["count"] == 1


def test_include_flagged_adds_the_scam_row(client):
    data = client.get("/analytics/neighborhoods?min_listings=1&include_flagged=true").json()
    assert data["excluded_flagged"] is False
    wb = next(n for n in data["neighborhoods"] if n["neighborhood"] == "Williamsburg")
    assert wb["total"] == 4
