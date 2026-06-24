"""SQLAlchemy ORM models for NYC rental listings and their rent history.

JSON column maps to JSONB on PostgreSQL (Neon) and to JSON/TEXT on SQLite, so
the same models run locally and in production unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Rental(Base):
    """One NYC apartment-rental listing, keyed by its source posting id."""

    __tablename__ = "rentals"

    # Stable unique posting id from the source (Craigslist `pid`).
    pid: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="craigslist", index=True)

    title: Mapped[str | None] = mapped_column(String(512))
    # Free-text neighborhood as posted on the source (messy, user-entered).
    neighborhood: Mapped[str | None] = mapped_column(String(255))
    # Canonical neighborhood resolved from lat/long against NYC boundaries
    # (see home_hunter.geo). This is what the map filter queries on.
    neighborhood_key: Mapped[str | None] = mapped_column(String(128), index=True)
    borough: Mapped[str | None] = mapped_column(String(64), index=True)

    # Content fingerprint shared by Craigslist reposts of the same apartment
    # (see home_hunter.dedup). Lets the upsert collapse a repost arriving under a
    # new pid into the existing row instead of storing it again. NULL when a
    # listing is too thin to fingerprint safely.
    dedup_key: Mapped[str | None] = mapped_column(String(40), index=True)

    # Scam heuristics (see home_hunter.flags). `flagged` is set on upsert and in
    # the end-of-run market pass; `flag_reasons` holds the human-readable signals
    # (e.g. ["no photos"]) for the UI badge. Listings are flagged, never dropped.
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    flag_reasons: Mapped[list] = mapped_column(JSON, default=list)

    price: Mapped[int | None] = mapped_column(Integer, index=True)  # monthly rent USD
    beds: Mapped[float | None] = mapped_column(Float, index=True)
    baths: Mapped[float | None] = mapped_column(Float)
    sqft: Mapped[int | None] = mapped_column(Integer, index=True)
    housing_type: Mapped[str | None] = mapped_column(String(64))

    # Amenities / details.
    laundry: Mapped[str | None] = mapped_column(String(64))
    parking: Mapped[str | None] = mapped_column(String(64))
    cats_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dogs_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    furnished: Mapped[bool] = mapped_column(Boolean, default=False)
    no_smoking: Mapped[bool] = mapped_column(Boolean, default=False)
    wheelchair_accessible: Mapped[bool] = mapped_column(Boolean, default=False)
    air_conditioning: Mapped[bool] = mapped_column(Boolean, default=False)
    ev_charging: Mapped[bool] = mapped_column(Boolean, default=False)
    no_fee: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rent_period: Mapped[str | None] = mapped_column(String(32))
    # Catch-all list of raw amenity labels not promoted to their own column.
    amenities: Mapped[list] = mapped_column(JSON, default=list)
    # Photo count from the detail page (0 = none; NULL when no detail was fetched).
    image_count: Mapped[int | None] = mapped_column(Integer)

    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str | None] = mapped_column(String(512))

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_scraped: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    rent_history: Mapped[list["RentHistory"]] = relationship(
        back_populates="rental",
        cascade="all, delete-orphan",
        order_by="RentHistory.observed_at",
    )


class RentHistory(Base):
    """A row is appended only when a listing's rent changes, building a trend."""

    __tablename__ = "rent_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pid: Mapped[str] = mapped_column(
        ForeignKey("rentals.pid", ondelete="CASCADE"), index=True
    )
    price: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )

    rental: Mapped[Rental] = relationship(back_populates="rent_history")
