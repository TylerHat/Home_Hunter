"""FastAPI over the scraped NYC rentals.

Read-only query layer for the search UI, plus a single write/trigger endpoint —
``POST /rescan`` — that wipes the database and re-scrapes in a background thread,
with ``GET /rescan/status`` exposing live progress. Run it with:
    uvicorn home_hunter.api.app:app --reload --app-dir src
Then browse the auto docs at http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import geo, pipeline
from ..analytics import RentalStatRow, neighborhood_rent_stats
from ..config import load_config
from ..db import UpsertStats, clear_all, get_sessionmaker, init_db, make_engine
from ..models import Rental, RentHistory, utcnow
from ..scraper import ACTIVE_SOURCES

logger = logging.getLogger(__name__)

app = FastAPI(title="Home Hunter — NYC Rentals API", version="0.2.0")

_STATIC_DIR = Path(__file__).parent / "static"

_engine = make_engine(load_config())
init_db(_engine)
_Session = get_sessionmaker(_engine)


def get_session() -> Session:
    with _Session() as session:
        yield session


# ---------------------------------------------------------------------------
# Rescan job: a single, in-process background scrape with live progress.
#
# This is the one endpoint that mutates data. The app is a single-user local
# tool, so one in-memory job record (guarded by a lock) is enough — there's no
# need for a job queue or persistence. ``_rescan`` is the snapshot that
# ``GET /rescan/status`` returns and the UI polls.
# ---------------------------------------------------------------------------
_rescan_lock = threading.Lock()
_rescan_thread: threading.Thread | None = None


def _idle_rescan_state() -> dict:
    return {
        "status": "idle",        # idle | running | done | error
        "phase": None,           # deleting | scraping | finalizing | done
        "deleted": 0,            # rows wiped before re-scraping
        "found": 0,              # listings enriched so far (cumulative, all sources)
        "current_source": None,  # source being pulled now (craigslist | renthop | …)
        "sources_total": 0,      # number of sources the sweep visits
        "sources_done": 0,       # sources fully scraped so far
        "current_area": None,
        "areas_total": 0,
        "areas_done": 0,
        "area_total": None,      # listings in the current borough (denominator)
        "area_found": 0,
        "progress": 0.0,         # 0..1, derived for the progress bar
        "error": None,
        "started_at": None,
        "finished_at": None,
        "stats": None,           # UpsertStats summary once done
    }


_rescan: dict = _idle_rescan_state()


def _recompute_progress(state: dict) -> None:
    """Source- then borough-weighted fraction for the progress bar.

    Each source is an equal slice of the bar; within a source the boroughs are
    equal slices, smoothed by the current borough's listing count.
    """
    sources_total = state["sources_total"] or 1
    areas_total = state["areas_total"] or 1
    area_frac = 0.0
    if state["area_total"]:
        area_frac = min(state["area_found"] / state["area_total"], 1.0)
    within_source = min((state["areas_done"] + area_frac) / areas_total, 1.0)
    overall = (state["sources_done"] + within_source) / sources_total
    # Hold just below 1.0 until the run actually finishes.
    state["progress"] = min(overall, 0.999)


def _apply_rescan_event(event: dict) -> None:
    """Fold one pipeline progress event into the shared rescan state."""
    with _rescan_lock:
        kind = event["type"]
        if kind == "area_start":
            _rescan["current_area"] = event["area"]
            _rescan["areas_total"] = event["total"]
            _rescan["area_total"] = None
            _rescan["area_found"] = 0
        elif kind == "summaries":
            _rescan["area_total"] = event["count"]
        elif kind == "listing":
            _rescan["found"] += 1
            _rescan["area_found"] += 1
        elif kind == "area_done":
            _rescan["areas_done"] += 1
            _rescan["area_total"] = None
            _rescan["area_found"] = 0
        elif kind == "finalizing":
            _rescan["phase"] = "finalizing"
        _recompute_progress(_rescan)


def _run_rescan() -> None:
    """Wipe the DB once, then re-scrape every source in turn (Craigslist, then
    RentHop, then any future source), updating ``_rescan`` as progress arrives."""
    try:
        config = load_config()
        sources = list(ACTIVE_SOURCES)
        with _rescan_lock:
            _rescan["phase"] = "deleting"
            _rescan["sources_total"] = len(sources)
            _rescan["areas_total"] = len(config.areas)
        with _Session() as session:
            deleted = clear_all(session)
            session.commit()
        with _rescan_lock:
            _rescan["deleted"] = deleted
            _rescan["phase"] = "scraping"

        total = UpsertStats()
        for source in sources:
            # Reset the per-source counters so the borough fraction restarts, and
            # name the source the UI status bar shows it's pulling from.
            with _rescan_lock:
                _rescan["current_source"] = source
                _rescan["phase"] = "scraping"
                _rescan["current_area"] = None
                _rescan["areas_done"] = 0
                _rescan["area_total"] = None
                _rescan["area_found"] = 0
                _recompute_progress(_rescan)
            total += pipeline.run(
                replace(config, source=source), on_progress=_apply_rescan_event
            )
            with _rescan_lock:
                _rescan["sources_done"] += 1
                _recompute_progress(_rescan)

        with _rescan_lock:
            _rescan["status"] = "done"
            _rescan["phase"] = "done"
            _rescan["progress"] = 1.0
            _rescan["finished_at"] = utcnow().isoformat()
            _rescan["stats"] = {
                "inserted": total.inserted,
                "updated": total.updated,
                "duplicates_merged": total.duplicates_merged,
                "price_changes": total.price_changes,
            }
    except Exception as exc:  # surface failure to the UI rather than dying silently
        logger.exception("rescan failed")
        with _rescan_lock:
            _rescan["status"] = "error"
            _rescan["error"] = str(exc)
            _rescan["finished_at"] = utcnow().isoformat()


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
    rent_stabilized: bool
    rent_stabilized_confirmed: bool | None
    amenities: list
    image_count: int | None
    flagged: bool
    flag_reasons: list | None
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


@app.post("/rescan")
def start_rescan() -> dict:
    """Wipe the database and re-scrape every source in a background thread.

    Sweeps all wired sources in order (Craigslist, then RentHop, …), each across
    every borough. Returns ``409`` if a rescan is already running. The UI then
    polls ``GET /rescan/status`` for live progress (incl. the current source).
    """
    global _rescan_thread
    with _rescan_lock:
        if _rescan["status"] == "running":
            raise HTTPException(status_code=409, detail="rescan already running")
        _rescan.clear()
        _rescan.update(_idle_rescan_state())
        _rescan["status"] = "running"
        _rescan["started_at"] = utcnow().isoformat()
    _rescan_thread = threading.Thread(target=_run_rescan, daemon=True)
    _rescan_thread.start()
    return {"status": "running"}


@app.get("/rescan/status")
def rescan_status() -> dict:
    """Current rescan job snapshot (idle/running/done/error + progress)."""
    with _rescan_lock:
        return dict(_rescan)


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


@app.get("/analytics/neighborhoods")
def analytics_neighborhoods(
    session: Annotated[Session, Depends(get_session)],
    borough: str | None = None,
    min_listings: Annotated[int, Query(ge=1)] = 1,
    include_flagged: bool = False,
) -> dict:
    """Per-neighborhood rent stats (studio/1/2/3+ averages, median, $/ft², …)."""
    stmt = select(
        Rental.neighborhood_key,
        Rental.borough,
        Rental.beds,
        Rental.price,
        Rental.sqft,
        Rental.no_fee,
    ).where(Rental.neighborhood_key.is_not(None), Rental.price.is_not(None))
    if borough is not None:
        stmt = stmt.where(Rental.borough == borough)
    if not include_flagged:
        stmt = stmt.where(Rental.flagged.is_(False))
    rows = [RentalStatRow(*r) for r in session.execute(stmt).all()]
    return {
        "neighborhoods": neighborhood_rent_stats(rows, min_listings=min_listings),
        "excluded_flagged": not include_flagged,
    }


@app.get("/rentals", response_model=list[RentalOut])
def list_rentals(
    session: Annotated[Session, Depends(get_session)],
    borough: str | None = None,
    source: str | None = None,
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
    rent_stabilized: bool = False,
    hide_flagged: bool = False,
    sort: str = "price",
    order: str = "asc",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Rental]:
    stmt = select(Rental)
    if borough is not None:
        stmt = stmt.where(Rental.borough == borough)
    if source is not None:
        stmt = stmt.where(Rental.source == source)
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
    if rent_stabilized:
        # Either the listing text advertises it or DHCR confirms it — matches the
        # green badge the UI shows for the same condition.
        stmt = stmt.where(
            or_(
                Rental.rent_stabilized.is_(True),
                Rental.rent_stabilized_confirmed.is_(True),
            )
        )
    if hide_flagged:
        stmt = stmt.where(Rental.flagged.is_(False))
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
