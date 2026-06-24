"""Recompute scam flags for rentals already in the database.

New scrapes flag listings on upsert and again in an end-of-run market pass (see
``home_hunter.flags`` and ``home_hunter.db.recompute_market_flags``). This
one-off applies the same logic to rows already stored — useful after changing
the ``flags:`` thresholds in config.yaml.

Idempotent — safe to re-run.

    python scripts/recompute_flags.py            # local SQLite (or DATABASE_URL)
    python scripts/recompute_flags.py --dry-run  # report counts, write nothing

Caveat: the dominant signal is **photo count**, which is only captured from
detail pages. Rows scraped before this feature have ``image_count = NULL`` and
so won't pick up the no-photos flag until they're re-scraped
(``python scripts/run_scrape.py``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script: add ./src to the import path.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from statistics import median  # noqa: E402

from sqlalchemy import select  # noqa: E402

from home_hunter import flags  # noqa: E402
from home_hunter.config import load_config  # noqa: E402
from home_hunter.db import get_sessionmaker, init_db, make_engine  # noqa: E402
from home_hunter.models import Rental  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report changes without writing"
    )
    args = parser.parse_args()

    config = load_config()
    settings = config.flags
    engine = make_engine(config)
    init_db(engine)  # ensures the flag columns exist
    Session = get_sessionmaker(engine)

    with Session() as session:
        rows = list(session.scalars(select(Rental)))

        cohorts: dict[tuple, list[int]] = {}
        for row in rows:
            if row.price is not None:
                cohorts.setdefault((row.borough, row.beds), []).append(row.price)
        medians = {
            cohort: median(prices)
            for cohort, prices in cohorts.items()
            if len(prices) >= settings.min_cohort_size
        }

        flagged = changed = no_photos = 0
        for row in rows:
            result = flags.evaluate(
                row, settings=settings, market_median=medians.get((row.borough, row.beds))
            )
            flagged += int(result.flagged)
            no_photos += int(row.image_count == 0)
            if row.flagged != result.flagged or list(row.flag_reasons or []) != result.reasons:
                changed += 1
                if not args.dry_run:
                    row.flagged = result.flagged
                    row.flag_reasons = result.reasons

        if not args.dry_run:
            session.commit()

    verb = "would update" if args.dry_run else "updated"
    print(
        f"{len(rows)} rentals: {flagged} flagged "
        f"({no_photos} with no photos), {verb} {changed} row(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
