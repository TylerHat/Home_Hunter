"""Backfill ``neighborhood_key`` for existing rentals from their coordinates.

New scrapes set ``neighborhood_key`` on upsert, but rows stored before the
column existed need a one-off pass. Idempotent — safe to re-run (e.g. after
updating the bundled neighborhood boundaries).

    python scripts/backfill_neighborhoods.py            # local SQLite (or DATABASE_URL)
    python scripts/backfill_neighborhoods.py --dry-run  # report changes, write nothing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script: add ./src to the import path.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sqlalchemy import select  # noqa: E402

from home_hunter import geo  # noqa: E402
from home_hunter.config import load_config  # noqa: E402
from home_hunter.db import get_sessionmaker, init_db, make_engine  # noqa: E402
from home_hunter.models import Rental  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report changes without writing"
    )
    args = parser.parse_args()

    engine = make_engine(load_config())
    init_db(engine)  # also ensures the neighborhood_key column exists
    Session = get_sessionmaker(engine)

    changed = tagged = total = 0
    with Session() as session:
        for row in session.scalars(select(Rental)):
            total += 1
            new_key = geo.neighborhood_for(row.latitude, row.longitude)
            if new_key:
                tagged += 1
            if new_key != row.neighborhood_key:
                changed += 1
                if not args.dry_run:
                    row.neighborhood_key = new_key
        if not args.dry_run:
            session.commit()

    verb = "would change" if args.dry_run else "changed"
    print(f"{total} rentals: {tagged} tagged with a neighborhood, {verb} {changed}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
