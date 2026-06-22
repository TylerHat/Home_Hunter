"""Database engine, schema init, and upsert-with-rent-history logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Config
from .models import Base, Rental, RentHistory, utcnow
from .scraper.craigslist.parse import RentalListing

logger = logging.getLogger(__name__)

# Columns copied from a scraped RentalListing onto the ORM row.
_RENTAL_FIELDS = (
    "source", "title", "neighborhood", "borough", "price", "beds", "baths",
    "sqft", "housing_type", "laundry", "parking", "cats_ok", "dogs_ok",
    "furnished", "no_smoking", "wheelchair_accessible", "air_conditioning",
    "ev_charging", "no_fee", "rent_period", "amenities", "latitude",
    "longitude", "url", "posted_at", "updated_at",
)


def make_engine(config: Config) -> Engine:
    """Create an engine from the resolved database URL (Postgres/Neon or SQLite)."""
    url = config.database_url
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)
    return engine


def init_db(engine: Engine) -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    price_changes: int = 0

    def __add__(self, other: "UpsertStats") -> "UpsertStats":
        return UpsertStats(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.price_changes + other.price_changes,
        )


def upsert_listing(session: Session, listing: RentalListing) -> UpsertStats:
    """Insert or update one rental; append rent history on a rent change."""
    stats = UpsertStats()
    now = utcnow()
    row = session.get(Rental, listing.pid)

    if row is None:
        row = Rental(pid=listing.pid, first_seen=now)
        for field in _RENTAL_FIELDS:
            setattr(row, field, getattr(listing, field))
        row.raw = listing.raw
        row.last_seen = now
        row.last_scraped = now
        session.add(row)
        if listing.price is not None:
            session.add(RentHistory(pid=listing.pid, price=listing.price, observed_at=now))
            stats.price_changes += 1
        stats.inserted += 1
        return stats

    # Existing rental: detect a rent change before overwriting.
    if listing.price is not None and row.price != listing.price:
        session.add(RentHistory(pid=listing.pid, price=listing.price, observed_at=now))
        stats.price_changes += 1

    for field in _RENTAL_FIELDS:
        value = getattr(listing, field)
        if value is not None:
            setattr(row, field, value)
    row.raw = listing.raw
    row.last_seen = now
    row.last_scraped = now
    stats.updated += 1
    return stats


def upsert_listings(session: Session, listings: list[RentalListing]) -> UpsertStats:
    """Upsert a batch of listings within the given session (caller commits)."""
    total = UpsertStats()
    for listing in listings:
        total += upsert_listing(session, listing)
    return total
