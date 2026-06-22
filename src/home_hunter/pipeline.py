"""Orchestrate a scrape run: for each borough -> scrape -> upsert -> commit."""

from __future__ import annotations

import logging

from .config import Config, load_config
from .db import UpsertStats, get_sessionmaker, init_db, make_engine, upsert_listings
from .scraper import build_client
from .scraper.craigslist import RentalListing, search_area

logger = logging.getLogger(__name__)


def store_listings(config: Config, listings: list[RentalListing]) -> UpsertStats:
    """Initialize the DB (if needed) and upsert a batch of listings."""
    engine = make_engine(config)
    init_db(engine)
    Session = get_sessionmaker(engine)
    with Session() as session:
        stats = upsert_listings(session, listings)
        session.commit()
    return stats


def run(
    config: Config | None = None,
    *,
    only_area: str | None = None,
) -> UpsertStats:
    """Run the full scrape across configured boroughs and persist results."""
    config = config or load_config()
    areas = [only_area] if only_area else config.areas
    if not areas:
        logger.warning("no areas configured — nothing to do")
        return UpsertStats()

    engine = make_engine(config)
    init_db(engine)
    Session = get_sessionmaker(engine)

    total = UpsertStats()
    with build_client(config) as client:
        for area in areas:
            try:
                listings = search_area(client, config, area)
            except Exception:  # one borough failing must not abort the whole run
                logger.exception("area %s failed — continuing", area)
                continue
            if not listings:
                continue
            with Session() as session:
                stats = upsert_listings(session, listings)
                session.commit()
            total += stats
            logger.info(
                "%s stored: +%d new, %d updated, %d rent changes",
                config.area_name(area), stats.inserted, stats.updated, stats.price_changes,
            )

    logger.info(
        "run complete: +%d new, %d updated, %d rent changes",
        total.inserted, total.updated, total.price_changes,
    )
    return total
