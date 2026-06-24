"""Content fingerprint that collapses Craigslist reposts of one apartment.

Craigslist users routinely delete and re-post the same listing to bump it back
to the top of search results; every repost is assigned a brand-new posting id
(``pid``). Because storage is keyed on ``pid``, the same physical apartment
otherwise lands in the database many times over (see the duplicate clusters that
motivated the ``1005-deleting-duplicates`` work).

A *fingerprint* identifies the underlying apartment across reposts from the
fields that stay constant on a repost — its normalized title, monthly rent,
bedroom count, and borough. ``db.upsert_listing`` uses it to recognise a repost
arriving under a new ``pid`` and update the existing row instead of inserting a
duplicate.

The fingerprint is deliberately conservative — it only collapses listings whose
titles are essentially identical. Coordinates are intentionally **not** part of
the key: brokers and new-developments share a single map pin across many
genuinely distinct units, so a coordinate-based key over-merges (e.g. four
different 3BR floor-plans at one address, or one management company's office pin
covering a whole neighborhood). The cost of this caution is that a repost which
also changes its price or title is treated as a new listing — a few extra rows,
which is far safer than silently hiding distinct apartments.
"""

from __future__ import annotations

import hashlib
import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str | None) -> str | None:
    """Lowercase, strip punctuation/emoji, and collapse whitespace.

    Reposts often differ only in casing or decorative characters, so the
    normalized form is what the fingerprint compares.
    """
    if not title:
        return None
    norm = _NON_ALNUM.sub(" ", title.lower()).strip()
    return norm or None


def fingerprint(
    *,
    title: str | None,
    price: int | None,
    beds: float | None,
    borough: str | None = None,
    source: str = "craigslist",
) -> str | None:
    """A stable key shared by reposts of one apartment, or ``None`` if too thin.

    Returns ``None`` when there isn't enough signal to dedupe safely (no title or
    no price). Such listings are only ever keyed by their ``pid``, so they are
    never merged with anything.
    """
    norm = normalize_title(title)
    if norm is None or price is None:
        return None
    beds_part = "" if beds is None else f"{beds:g}"
    composite = "|".join([source or "", (borough or "").lower(), norm, str(price), beds_part])
    return hashlib.sha1(composite.encode("utf-8")).hexdigest()
