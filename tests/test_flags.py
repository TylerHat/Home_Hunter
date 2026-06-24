"""Offline unit tests for the scam-flag heuristics (pure, no DB)."""

from types import SimpleNamespace

from home_hunter.flags import FlagSettings, evaluate


def listing(**overrides):
    """A healthy listing by default; override a field to introduce a signal."""
    base = dict(
        image_count=6, price=3000, beds=1.0, latitude=40.7, longitude=-73.9,
        rent_period="monthly", title="Sunny 1BR in Astoria",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_healthy_listing_is_not_flagged():
    result = evaluate(listing())
    assert result.flagged is False
    assert result.reasons == []


def test_no_photos_flags_on_its_own():
    result = evaluate(listing(image_count=0))
    assert result.flagged is True
    assert "no photos" in result.reasons


def test_unknown_photo_count_is_not_penalized():
    # image_count=None means no detail page was fetched — don't flag the unknown.
    result = evaluate(listing(image_count=None))
    assert "no photos" not in result.reasons
    assert result.flagged is False


def test_implausible_rent_flags():
    result = evaluate(listing(price=2))  # classic teaser-spam price
    assert result.flagged is True
    assert "implausible rent" in result.reasons


def test_missing_location_alone_does_not_flag():
    result = evaluate(listing(latitude=None, longitude=None))
    assert "no map location" in result.reasons
    assert result.flagged is False  # weight below threshold on its own


def test_below_area_median_is_corroborating_not_decisive():
    # Far below the cohort median, but with photos + location it only warns.
    cheap = listing(price=1500)
    result = evaluate(cheap, market_median=4000)
    assert "rent far below area median" in result.reasons
    assert result.flagged is False
    # Same listing with no photos crosses the threshold and stacks both reasons.
    combined = evaluate(listing(price=1500, image_count=0), market_median=4000)
    assert combined.flagged is True
    assert {"no photos", "rent far below area median"} <= set(combined.reasons)


def test_no_market_context_skips_median_signal():
    result = evaluate(listing(price=1500))  # no market_median passed
    assert "rent far below area median" not in result.reasons


def test_weights_are_tunable_via_settings():
    lenient = FlagSettings(no_photo_weight=10, threshold=100)
    assert evaluate(listing(image_count=0), settings=lenient).flagged is False


def test_disabled_settings_never_flags():
    result = evaluate(listing(image_count=0), settings=FlagSettings(enabled=False))
    assert result.flagged is False
    assert result.reasons == []
