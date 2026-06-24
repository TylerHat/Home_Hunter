"""Collapse Craigslist reposts already stored as separate rows.

New scrapes dedupe on upsert (see ``home_hunter.dedup``), but rows written
before that existed sit in the database as duplicate reposts of the same
apartment. This one-off pass fingerprints every rental, then for each group of
reposts keeps the **most recently posted** row (the live listing) and deletes
the rest; their rent-history rows cascade away with them.

Idempotent — safe to re-run. After a clean pass it only ever sets missing
``dedup_key`` values and reports zero deletions.

    python scripts/dedupe_existing.py            # local SQLite (or DATABASE_URL)
    python scripts/dedupe_existing.py --dry-run  # report what it would remove, write nothing
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

# Allow running as a plain script: add ./src to the import path.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import select  # noqa: E402

from home_hunter import dedup  # noqa: E402
from home_hunter.config import load_config  # noqa: E402
from home_hunter.db import get_sessionmaker, init_db, make_engine  # noqa: E402
from home_hunter.models import Rental  # noqa: E402

# Reposts sort newest-first by posted_at; rows missing a date sort last.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _recency(row: Rental) -> tuple[datetime, str]:
    posted = row.posted_at or _EPOCH
    if posted.tzinfo is None:  # SQLite returns naive datetimes
        posted = posted.replace(tzinfo=timezone.utc)
    return (posted, row.pid)  # pid breaks ties deterministically


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="report changes without writing"
    )
    args = parser.parse_args()

    engine = make_engine(load_config())
    init_db(engine)  # ensures the dedup_key column + index exist
    Session = get_sessionmaker(engine)

    groups: dict[str, list[Rental]] = defaultdict(list)
    total = keyed = 0
    with Session() as session:
        for row in session.scalars(select(Rental)):
            total += 1
            key = dedup.fingerprint(
                title=row.title, price=row.price, beds=row.beds,
                borough=row.borough, source=row.source,
            )
            if row.dedup_key != key and not args.dry_run:
                row.dedup_key = key  # backfill so future upserts can match reposts
            if key is not None:
                keyed += 1
                groups[key].append(row)

        removed = 0
        for rows in groups.values():
            if len(rows) < 2:
                continue
            rows.sort(key=_recency, reverse=True)  # survivor = newest posting
            for stale in rows[1:]:
                removed += 1
                if not args.dry_run:
                    session.delete(stale)

        if not args.dry_run:
            session.commit()

    verb = "would remove" if args.dry_run else "removed"
    print(
        f"{total} rentals ({keyed} fingerprinted): "
        f"{verb} {removed} duplicate repost(s), "
        f"leaving {total - removed}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
