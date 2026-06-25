"""Orchestrate a scrape run: for each borough -> scrape -> upsert -> commit."""

from __future__ import annotations

import logging
from collections.abc import Callable

from .config import Config, load_config
from .db import (
    UpsertStats,
    get_sessionmaker,
    init_db,
    make_engine,
    recompute_market_flags,
    upsert_listings,
)
from .scraper import build_client
from .scraper.craigslist import RentalListing, search_area

logger = logging.getLogger(__name__)


def store_listings(config: Config, listings: list[RentalListing]) -> UpsertStats:
    """Initialize the DB (if needed) and upsert a batch of listings."""
    engine = make_engine(config)
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as session:
        stats = upsert_listings(session, listings, config.flags)
        recompute_market_flags(session, config.flags)
        session.commit()
    return stats


def _forward_area_events(
    on_progress: Callable[[dict], None], area: str
) -> Callable[[dict], None]:
    """Wrap ``on_progress`` so a borough's ``search_area`` events carry its name."""
    return lambda event: on_progress({**event, "area": area})


def run(
    config: Config | None = None,
    *,
    only_area: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> UpsertStats:
    """Run the full scrape across configured boroughs and persist results.

    ``on_progress`` (optional) receives progress events for the in-UI rescan:
    ``area_start``/``area_done`` per borough, ``summaries``/``listing`` events
    forwarded from ``search_area`` (with the ``area`` name added), ``finalizing``
    before the market-flag pass, and ``done`` at the end. It's a no-op seam when
    omitted, so CLI/test callers are unaffected.
    """
    config = config or load_config()
    areas = [only_area] if only_area else config.areas
    if not areas:
        logger.warning("no areas configured — nothing to do")
        return UpsertStats()

    engine = make_engine(config)
    init_db(engine)
    Session = get_sessionmaker(engine)

    def emit(event: dict) -> None:
        if on_progress is not None:
            on_progress(event)

    total = UpsertStats()
    with build_client(config) as client:
        for index, area in enumerate(areas):
            name = config.area_name(area)
            emit({"type": "area_start", "area": name, "index": index, "total": len(areas)})
            try:
                listings = search_area(
                    client, config, area,
                    on_event=_forward_area_events(emit, name),
                )
            except Exception:  # one borough failing must not abort the whole run
                logger.exception("area %s failed — continuing", area)
                emit({"type": "area_done", "area": name})
                continue
            if listings:
                with Session() as session:
                    stats = upsert_listings(session, listings, config.flags)
                    session.commit()
                total += stats
                logger.info(
                    "%s stored: +%d new, %d updated, %d reposts merged, %d rent changes",
                    name, stats.inserted, stats.updated,
                    stats.duplicates_merged, stats.price_changes,
                )
            emit({"type": "area_done", "area": name})

    # Re-evaluate scam flags once the whole dataset is present, so the
    # rent-vs-area-median signal sees accurate cohort medians.
    emit({"type": "finalizing"})
    with Session() as session:
        flagged = recompute_market_flags(session, config.flags)
        session.commit()
    logger.info("scam flags: %d listing(s) flagged", flagged)
    emit({"type": "done"})

    logger.info(
        "run complete: +%d new, %d updated, %d reposts merged, %d rent changes",
        total.inserted, total.updated, total.duplicates_merged, total.price_changes,
    )
    return total
