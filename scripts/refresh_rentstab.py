"""Refresh the bundled rent-stabilized BBL set used to confirm listings.

Downloads the deduped list of NYC BBLs (Borough-Block-Lot) that have
rent-stabilized residential units registered with NY State DHCR, and writes it
to ``src/home_hunter/rentstab/stabilized_bbls.txt`` (one 10-digit BBL per line).
That file is git-committed so scrapes and tests stay fully offline; this script
is a rare one-off, run only to update the vintage.

Provenance: clhenrick/dhcr-rent-stabilized-data (``csv/dhcr_unique_bbls.csv``),
compiled from the annual DHCR rent-stabilized building lists. BBL is the standard
join key across NYC housing datasets.

    python scripts/refresh_rentstab.py            # download + rewrite the bundle
    python scripts/refresh_rentstab.py --dry-run  # report counts, write nothing
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

# Allow running as a plain script: add ./src to the import path.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from home_hunter.rentstab import normalize_bbl  # noqa: E402

SOURCE_URL = (
    "https://raw.githubusercontent.com/clhenrick/dhcr-rent-stabilized-data/"
    "master/csv/dhcr_unique_bbls.csv"
)
OUT_PATH = SRC / "home_hunter" / "rentstab" / "stabilized_bbls.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    # truststore + httpx mirror the scraper's TLS handling behind a corp proxy.
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
    import httpx

    print(f"downloading {SOURCE_URL} ...")
    resp = httpx.get(SOURCE_URL, timeout=60, follow_redirects=True)
    resp.raise_for_status()

    bbls = sorted({b for line in resp.text.splitlines() if (b := normalize_bbl(line))})
    print(f"parsed {len(bbls)} unique 10-digit BBLs")
    if not bbls:
        print("ERROR: no BBLs parsed — refusing to overwrite the bundle")
        return 1

    if args.dry_run:
        print(f"(dry run) would write {len(bbls)} BBLs to {OUT_PATH}")
        return 0

    header = (
        f"# NYC rent-stabilized building BBLs (Borough-Block-Lot).\n"
        f"# Source: {SOURCE_URL}\n"
        f"# Refreshed: {_dt.date.today().isoformat()} - {len(bbls)} BBLs.\n"
        f"# Regenerate with: python scripts/refresh_rentstab.py\n"
    )
    OUT_PATH.write_text(header + "\n".join(bbls) + "\n", encoding="ascii")
    print(f"wrote {len(bbls)} BBLs to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
