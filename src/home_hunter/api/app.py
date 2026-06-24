"""Minimal read-only FastAPI over the scraped NYC rentals.

This is the backend seam the future search UI will consume. Run it with:
    uvicorn home_hunter.api.app:app --reload --app-dir src
Then browse the auto docs at http://127.0.0.1:8000/docs
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import geo
from ..config import load_config
from ..db import get_sessionmaker, init_db, make_engine
from ..models import Rental, RentHistory

app = FastAPI(title="Home Hunter — NYC Rentals API", version="0.2.0")

_STATIC_DIR = Path(__file__).parent / "static"

_engine = make_engine(load_config())
init_db(_engine)
_Session = get_sessionmaker(_engine)


def get_session() -> Session:
    with _Session() as session:
        yield session


class RentalOut(BaseModel):
    pid: str
    source: str
    title: str | None
    neighborhood: str | None
    neighborhood_key: str | None
    borough: str | None
    price: int | None
    beds: float | None
    baths: float | None
    sqft: int | None
    housing_type: str | None
    laundry: str | None
    parking: str | None
    cats_ok: bool
    dogs_ok: bool
    furnished: bool
    no_fee: bool
    amenities: list
    latitude: float | None
    longitude: float | None
    url: str | None
    posted_at: datetime | None
    last_seen: datetime

    model_config = {"from_attributes": True}


class RentHistoryOut(BaseModel):
    price: int
    observed_at: datetime

    model_config = {"from_attributes": True}


# Sortable columns exposed by GET /rentals. Keys match the table headers in the
# web UI; text columns are lowered so the sort is case-insensitive, and the
# neighborhood sort coalesces to the same value the UI displays.
_SORT_COLUMNS = {
    "price": Rental.price,
    "title": func.lower(Rental.title),
    "beds": Rental.beds,
    "baths": Rental.baths,
    "sqft": Rental.sqft,
    "neighborhood": func.lower(func.coalesce(Rental.neighborhood_key, Rental.neighborhood)),
    "borough": Rental.borough,
    "posted_at": Rental.posted_at,
}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the rental-browser web page."""
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/neighborhoods.geojson")
def neighborhoods_geojson() -> Response:
    """NYC neighborhood boundaries the map UI renders as clickable shapes."""
    return Response(content=geo.geojson_text(), media_type="application/geo+json")


@app.get("/stats")
def stats(session: Annotated[Session, Depends(get_session)]) -> dict:
    """Summary counts for the page header."""
    total = session.scalar(select(func.count()).select_from(Rental)) or 0
    avg = session.scalar(select(func.avg(Rental.price)))
    rows = session.execute(
        select(Rental.borough, func.count())
        .where(Rental.borough.is_not(None))
        .group_by(Rental.borough)
        .order_by(func.count().desc())
    ).all()
    nb_rows = session.execute(
        select(Rental.neighborhood_key, func.count())
        .where(Rental.neighborhood_key.is_not(None))
        .group_by(Rental.neighborhood_key)
    ).all()
    return {
        "total": total,
        "rent": {
            "min": session.scalar(select(func.min(Rental.price))),
            "avg": round(avg) if avg is not None else None,
            "max": session.scalar(select(func.max(Rental.price))),
        },
        "by_borough": {b: c for b, c in rows},
        "by_neighborhood": {n: c for n, c in nb_rows},
    }


@app.get("/rentals", response_model=list[RentalOut])
def list_rentals(
    session: Annotated[Session, Depends(get_session)],
    borough: str | None = None,
    neighborhood: Annotated[list[str] | None, Query()] = None,
    min_rent: int | None = None,
    max_rent: int | None = None,
    min_beds: float | None = None,
    max_beds: float | None = None,
    min_sqft: int | None = None,
    housing_type: str | None = None,
    cats_ok: bool | None = None,
    dogs_ok: bool | None = None,
    no_fee: bool | None = None,
    sort: str = "price",
    order: str = "asc",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Rental]:
    stmt = select(Rental)
    if borough is not None:
        stmt = stmt.where(Rental.borough == borough)
    if neighborhood:
        stmt = stmt.where(Rental.neighborhood_key.in_(neighborhood))
    if min_rent is not None:
        stmt = stmt.where(Rental.price >= min_rent)
    if max_rent is not None:
        stmt = stmt.where(Rental.price <= max_rent)
    if min_beds is not None:
        stmt = stmt.where(Rental.beds >= min_beds)
    if max_beds is not None:
        stmt = stmt.where(Rental.beds <= max_beds)
    if min_sqft is not None:
        stmt = stmt.where(Rental.sqft >= min_sqft)
    if housing_type is not None:
        stmt = stmt.where(Rental.housing_type == housing_type)
    if cats_ok is not None:
        stmt = stmt.where(Rental.cats_ok == cats_ok)
    if dogs_ok is not None:
        stmt = stmt.where(Rental.dogs_ok == dogs_ok)
    if no_fee is not None:
        stmt = stmt.where(Rental.no_fee == no_fee)
    col = _SORT_COLUMNS.get(sort, Rental.price)
    ordering = col.desc() if order == "desc" else col.asc()
    # pid is a stable tiebreaker so paging (offset/Load more) never skips or
    # repeats rows that share a sort value.
    stmt = stmt.order_by(ordering.nulls_last(), Rental.pid.asc()).limit(limit).offset(offset)
    return list(session.scalars(stmt).all())


@app.get("/rentals/{pid}", response_model=RentalOut)
def get_rental(pid: str, session: Annotated[Session, Depends(get_session)]) -> Rental:
    rental = session.get(Rental, pid)
    if rental is None:
        raise HTTPException(status_code=404, detail="rental not found")
    return rental


@app.get("/rentals/{pid}/rent-history", response_model=list[RentHistoryOut])
def get_rent_history(
    pid: str, session: Annotated[Session, Depends(get_session)]
) -> list[RentHistory]:
    stmt = (
        select(RentHistory)
        .where(RentHistory.pid == pid)
        .order_by(RentHistory.observed_at.asc())
    )
    return list(session.scalars(stmt).all())
