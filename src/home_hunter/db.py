"""Database engine, schema init, and upsert-with-rent-history logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from statistics import median

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from . import dedup, flags, geo
from .config import Config
from .flags import FlagSettings
from .models import Base, Rental, RentHistory, utcnow
from .scraper.craigslist.parse import RentalListing

logger = logging.getLogger(__name__)

# Columns copied from a scraped RentalListing onto the ORM row.
_RENTAL_FIELDS = (
    "source", "title", "neighborhood", "borough", "price", "beds", "baths",
    "sqft", "housing_type", "laundry", "parking", "cats_ok", "dogs_ok",
    "furnished", "no_smoking", "wheelchair_accessible", "air_conditioning",
    "ev_charging", "no_fee", "rent_stabilized", "rent_period", "amenities", "image_count",
    "latitude", "longitude", "url", "posted_at", "updated_at",
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
    """Create tables if they don't exist, then apply lightweight migrations."""
    Base.metadata.create_all(engine)
    _ensure_columns(engine)


def _ensure_columns(engine: Engine) -> None:
    """Add columns introduced after a DB was first created (no Alembic).

    ``create_all`` never alters existing tables, so a DB built before a column
    existed needs a one-off ``ADD COLUMN``. Idempotent on SQLite and Postgres.
    """
    insp = inspect(engine)
    if "rentals" not in insp.get_table_names():
        return
    columns = {col["name"] for col in insp.get_columns("rentals")}
    if "neighborhood_key" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rentals ADD COLUMN neighborhood_key VARCHAR(128)"))
    if "dedup_key" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rentals ADD COLUMN dedup_key VARCHAR(40)"))
    if "image_count" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rentals ADD COLUMN image_count INTEGER"))
    # Scam-flag columns. Constant defaults backfill existing rows (FALSE / '[]')
    # on both SQLite and Postgres, so the columns are never NULL.
    if "flagged" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rentals ADD COLUMN flagged BOOLEAN DEFAULT FALSE"))
    if "flag_reasons" not in columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE rentals ADD COLUMN flag_reasons JSON DEFAULT '[]'"))
    # Rent-stabilized marker. Constant FALSE default backfills existing rows; the
    # photo-style text signal needs a re-scrape to populate old rows.
    if "rent_stabilized" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE rentals ADD COLUMN rent_stabilized BOOLEAN DEFAULT FALSE")
            )
    # Index lookups used by dedup and the hide-flagged filter (idempotent; valid
    # on both SQLite and Postgres).
    with engine.begin() as conn:
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_rentals_dedup_key ON rentals (dedup_key)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_rentals_flagged ON rentals (flagged)")
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_rentals_rent_stabilized ON rentals (rent_stabilized)")
        )


def get_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    price_changes: int = 0
    duplicates_merged: int = 0  # reposts folded into an existing row by fingerprint

    def __add__(self, other: "UpsertStats") -> "UpsertStats":
        return UpsertStats(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.price_changes + other.price_changes,
            self.duplicates_merged + other.duplicates_merged,
        )


def _listing_dedup_key(listing: RentalListing) -> str | None:
    return dedup.fingerprint(
        title=listing.title,
        price=listing.price,
        beds=listing.beds,
        borough=listing.borough,
        source=listing.source,
    )


def _find_repost(session: Session, key: str | None) -> Rental | None:
    """The existing row a fingerprint maps to (the most recently seen, if any)."""
    if not key:
        return None
    stmt = (
        select(Rental)
        .where(Rental.dedup_key == key)
        .order_by(Rental.last_seen.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _apply_intrinsic_flags(row: Rental, settings: FlagSettings | None) -> None:
    """Set scam flags from a row's own fields (no market context — see
    ``recompute_market_flags`` for the median-relative signal)."""
    result = flags.evaluate(row, settings=settings)
    row.flagged = result.flagged
    row.flag_reasons = result.reasons


def upsert_listing(
    session: Session, listing: RentalListing, settings: FlagSettings | None = None
) -> UpsertStats:
    """Insert or update one rental; append rent history on a rent change.

    A listing arriving under a new ``pid`` but matching an existing row's
    fingerprint (see ``home_hunter.dedup``) is a Craigslist repost: it updates
    that row in place — keeping its original ``pid`` as the stable internal key —
    instead of creating a duplicate.
    """
    stats = UpsertStats()
    now = utcnow()
    key = _listing_dedup_key(listing)

    # Resolve the target row: exact pid first, else a repost under a new pid.
    row = session.get(Rental, listing.pid)
    is_repost = False
    if row is None:
        row = _find_repost(session, key)
        is_repost = row is not None

    if row is None:
        row = Rental(pid=listing.pid, first_seen=now)
        for field in _RENTAL_FIELDS:
            setattr(row, field, getattr(listing, field))
        row.dedup_key = key
        row.neighborhood_key = geo.neighborhood_for(row.latitude, row.longitude)
        _apply_intrinsic_flags(row, settings)
        row.raw = listing.raw
        row.last_seen = now
        row.last_scraped = now
        session.add(row)
        if listing.price is not None:
            session.add(RentHistory(pid=listing.pid, price=listing.price, observed_at=now))
            stats.price_changes += 1
        stats.inserted += 1
        return stats

    # Existing rental (same pid, or a repost matched by fingerprint): detect a
    # rent change before overwriting. History is keyed to the surviving row's
    # pid, not the (possibly different) incoming repost pid.
    if listing.price is not None and row.price != listing.price:
        session.add(RentHistory(pid=row.pid, price=listing.price, observed_at=now))
        stats.price_changes += 1

    for field in _RENTAL_FIELDS:
        value = getattr(listing, field)
        if value is not None:
            setattr(row, field, value)
    row.dedup_key = key or row.dedup_key
    row.neighborhood_key = geo.neighborhood_for(row.latitude, row.longitude)
    _apply_intrinsic_flags(row, settings)
    row.raw = listing.raw
    row.last_seen = now
    row.last_scraped = now
    if is_repost:
        stats.duplicates_merged += 1
    else:
        stats.updated += 1
    return stats


def upsert_listings(
    session: Session, listings: list[RentalListing], settings: FlagSettings | None = None
) -> UpsertStats:
    """Upsert a batch of listings within the given session (caller commits)."""
    total = UpsertStats()
    for listing in listings:
        total += upsert_listing(session, listing, settings)
    return total


def recompute_market_flags(session: Session, settings: FlagSettings | None = None) -> int:
    """Re-evaluate scam flags for every row with market context, returning the
    flagged count.

    Computes a median rent per ``(borough, beds)`` cohort from the full dataset
    and re-runs ``flags.evaluate`` on each row, so the "rent far below area
    median" signal (which needs the whole picture) is applied on top of the
    intrinsic signals set at upsert. Idempotent; the caller commits.
    """
    settings = settings or FlagSettings()
    rows = list(session.scalars(select(Rental)).all())

    cohorts: dict[tuple, list[int]] = {}
    for row in rows:
        if row.price is not None:
            cohorts.setdefault((row.borough, row.beds), []).append(row.price)
    medians = {
        cohort: median(prices)
        for cohort, prices in cohorts.items()
        if len(prices) >= settings.min_cohort_size
    }

    flagged = 0
    for row in rows:
        result = flags.evaluate(
            row, settings=settings, market_median=medians.get((row.borough, row.beds))
        )
        row.flagged = result.flagged
        row.flag_reasons = result.reasons
        flagged += int(result.flagged)
    return flagged
