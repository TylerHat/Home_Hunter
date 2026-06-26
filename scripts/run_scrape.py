"""Entrypoint for the NYC rental scrape (run locally or by the scheduled workflow).

Examples:
    python scripts/run_scrape.py                      # scrape all boroughs in config.yaml
    python scripts/run_scrape.py --area mnh           # scrape only Manhattan
    python scripts/run_scrape.py --area mnh --once --dry-run
    python scripts/run_scrape.py --area mnh --no-detail  # skip detail pages (fast)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a plain script: add ./src to the import path.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dataclasses import replace  # noqa: E402

from home_hunter.config import load_config  # noqa: E402
from home_hunter.pipeline import run  # noqa: E402
from home_hunter.scraper import build_client, search_area  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape NYC rentals into the database.")
    parser.add_argument("--area", dest="area", help="scrape only this Craigslist area code (e.g. mnh)")
    parser.add_argument("--once", action="store_true", help="scrape a single search page (overrides max_pages)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="scrape and print results without writing to the database",
    )
    parser.add_argument(
        "--no-detail", action="store_true",
        help="skip detail pages - search-page fields only (much faster)",
    )
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    if args.once:
        config = replace(config, filters=replace(config.filters, max_pages=1))
    if args.no_detail:
        config = replace(config, detail_fetch=False)

    if args.dry_run:
        areas = [args.area] if args.area else config.areas
        with build_client(config) as client:
            for area in areas:
                listings = search_area(client, config, area)
                print(f"\n{config.area_name(area)} ({area}): {len(listings)} listings")
                for r in listings[:10]:
                    amen = ", ".join(
                        f for f in (r.laundry, r.parking,
                                    "cats" if r.cats_ok else None,
                                    "dogs" if r.dogs_ok else None,
                                    "no-fee" if r.no_fee else None) if f
                    )
                    print(
                        f"  {r.pid}  ${r.price or '?':>6}  "
                        f"{r.beds if r.beds is not None else '?'}bd/"
                        f"{r.baths if r.baths is not None else '?'}ba  "
                        f"{(str(r.sqft) + 'ft²') if r.sqft else '?ft²':>7}  "
                        f"{r.neighborhood or '?'}  [{amen}]"
                    )
        return 0

    stats = run(config, only_area=args.area)
    print(
        f"Done: +{stats.inserted} new, {stats.updated} updated, "
        f"{stats.price_changes} rent changes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
