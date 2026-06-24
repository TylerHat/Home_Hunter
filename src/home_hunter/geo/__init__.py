"""Map a listing's coordinates to its NYC neighborhood.

Pure-Python, no geospatial dependencies. The bundled GeoJSON
(``nyc_neighborhoods.geojson`` — the colloquial *pediacities* NYC neighborhood
boundaries, e.g. "Upper East Side", "Williamsburg", "Flatiron District") is
loaded once and each ``(lat, lng)`` is resolved with a ray-casting
point-in-polygon test guarded by a per-neighborhood bounding-box fast path.

Reads only the bundled file — no network — so it is fully unit-testable offline,
in the same spirit as the planned ``scoring`` module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

GEOJSON_PATH = Path(__file__).parent / "nyc_neighborhoods.geojson"

# A linear ring as (lng, lat) pairs. GeoJSON stores coordinates lng-first.
Ring = list[tuple[float, float]]
# A polygon is its outer ring followed by any hole rings.
Polygon = list[Ring]
BBox = tuple[float, float, float, float]  # (min_lng, min_lat, max_lng, max_lat)


@dataclass(frozen=True)
class Neighborhood:
    name: str
    borough: str | None
    polygons: list[Polygon]
    bbox: BBox


def geojson_text() -> str:
    """Raw bundled GeoJSON, for the API to hand to the map UI."""
    return GEOJSON_PATH.read_text(encoding="utf-8")


def _bbox_of(polygons: list[Polygon]) -> BBox:
    xs_min = ys_min = float("inf")
    xs_max = ys_max = float("-inf")
    for poly in polygons:
        for lng, lat in poly[0]:  # outer ring bounds the whole polygon
            xs_min, xs_max = min(xs_min, lng), max(xs_max, lng)
            ys_min, ys_max = min(ys_min, lat), max(ys_max, lat)
    return (xs_min, ys_min, xs_max, ys_max)


@lru_cache(maxsize=1)
def _neighborhoods() -> tuple[Neighborhood, ...]:
    data = json.loads(geojson_text())
    out: list[Neighborhood] = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon":
            raw_polys = [coords]
        elif gtype == "MultiPolygon":
            raw_polys = coords
        else:
            continue  # null geometry (a few parks/islands) — nothing to test
        polygons: list[Polygon] = [
            [[(float(x), float(y)) for x, y in ring] for ring in poly]
            for poly in raw_polys
        ]
        props = feat.get("properties") or {}
        name = props.get("neighborhood")
        if not name:
            continue
        out.append(
            Neighborhood(
                name=name,
                borough=props.get("borough"),
                polygons=polygons,
                bbox=_bbox_of(polygons),
            )
        )
    return tuple(out)


def _point_in_ring(lng: float, lat: float, ring: Ring) -> bool:
    """Ray-casting (even-odd) test of a point against a single ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lng: float, lat: float, polygon: Polygon) -> bool:
    if not polygon or not _point_in_ring(lng, lat, polygon[0]):
        return False
    return not any(_point_in_ring(lng, lat, hole) for hole in polygon[1:])


def neighborhood_for(lat: float | None, lng: float | None) -> str | None:
    """Neighborhood name containing ``(lat, lng)``, or ``None`` if outside all."""
    if lat is None or lng is None:
        return None
    for nb in _neighborhoods():
        min_lng, min_lat, max_lng, max_lat = nb.bbox
        if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
            continue
        if any(_point_in_polygon(lng, lat, poly) for poly in nb.polygons):
            return nb.name
    return None


def list_neighborhoods() -> list[dict[str, str | None]]:
    """All neighborhoods as ``{"name", "borough"}``, sorted by borough then name."""
    items = [{"name": nb.name, "borough": nb.borough} for nb in _neighborhoods()]
    return sorted(items, key=lambda d: (d["borough"] or "", d["name"] or ""))
