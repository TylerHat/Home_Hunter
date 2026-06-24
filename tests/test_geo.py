"""Offline tests for neighborhood resolution against the bundled boundaries."""

import json

import pytest

from home_hunter import geo

# (label, lat, lng, expected neighborhood) — well-known points inside each.
KNOWN_POINTS = [
    ("Upper East Side", 40.7736, -73.9566, "Upper East Side"),
    ("Williamsburg", 40.7081, -73.9571, "Williamsburg"),
    ("Flatiron District", 40.7401, -73.9903, "Flatiron District"),
    ("Astoria", 40.7644, -73.9235, "Astoria"),
]


@pytest.mark.parametrize("label,lat,lng,expected", KNOWN_POINTS)
def test_known_points_resolve(label, lat, lng, expected):
    assert geo.neighborhood_for(lat, lng) == expected


def test_point_outside_all_polygons_is_none():
    assert geo.neighborhood_for(40.50, -73.90) is None  # out in the harbor


def test_missing_coordinates_are_none():
    assert geo.neighborhood_for(None, None) is None
    assert geo.neighborhood_for(40.7, None) is None
    assert geo.neighborhood_for(None, -73.9) is None


def test_list_neighborhoods_covers_all_boroughs():
    items = geo.list_neighborhoods()
    assert len(items) > 250
    names = {it["name"] for it in items}
    assert {"Upper East Side", "Williamsburg", "Astoria"} <= names
    assert {it["borough"] for it in items} >= {
        "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"
    }


def test_geojson_text_is_valid_feature_collection():
    data = json.loads(geo.geojson_text())
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) > 250
