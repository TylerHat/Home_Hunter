"""Authoritative rent-stabilized lookup from a bundled BBL set (pure, offline).

A building's **BBL** (Borough-Block-Lot, 10 digits) is the standard join key
across NYC housing data. ``stabilized_bbls.txt`` is the deduped set of BBLs with
rent-stabilized units registered with NY State DHCR (see
``scripts/refresh_rentstab.py`` for provenance + refresh). ``is_stabilized(bbl)``
answers the authoritative question the listing's free text only *guesses* at
(the existing text-derived ``rent_stabilized`` flag).

This module is pure and offline — like ``home_hunter.geo`` the data is bundled
and there is no I/O beyond reading the file once. Address→BBL resolution (which
*does* need the network) lives in ``home_hunter.rentstab.geocode``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).with_name("stabilized_bbls.txt")


def normalize_bbl(value: str | int | None) -> str | None:
    """A BBL as a 10-digit string, or ``None`` if it can't be one.

    Accepts ints or strings — GeoSearch returns e.g. ``1010580030`` — strips any
    non-digits, and requires exactly 10 digits (1 borough + 5 block + 4 lot).
    """
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits if len(digits) == 10 else None


@lru_cache(maxsize=1)
def _stabilized_set() -> frozenset[str]:
    """The bundled BBL set, loaded once. ``#`` comment lines are ignored."""
    try:
        text = _DATA.read_text(encoding="ascii")
    except OSError:
        return frozenset()
    out: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        bbl = normalize_bbl(line)
        if bbl:
            out.add(bbl)
    return frozenset(out)


def is_stabilized(bbl: str | int | None) -> bool:
    """True if ``bbl`` is a building with DHCR-registered rent-stabilized units."""
    normalized = normalize_bbl(bbl)
    return normalized is not None and normalized in _stabilized_set()


def stabilized_count() -> int:
    """Number of BBLs in the bundled set (0 if the bundle is missing)."""
    return len(_stabilized_set())
