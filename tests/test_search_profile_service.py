import pytest

from app import create_app, db
from services.search_profile_service import (
    SearchProfileService,
    extract_search_name,
    normalize_travel_targets_config,
)
from tests import setup_test_environment


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def test_extract_search_name_from_subject():
    subject = "New detached house in your search: Search Junio!"
    body = ""
    assert extract_search_name(subject, body) == "Junio"


def test_extract_search_name_without_search_prefix():
    subject = 'New detached house in your search: "Homes in Ciudad Quesada"'
    body = 'See all listings for "Homes in Ciudad Quesada"'
    assert extract_search_name(subject, body) == "Homes in Ciudad Quesada"


def test_extract_search_name_from_subject_without_quotes():
    subject = "New home in your search: Homes in San Juan de Alicante, Alicante!"
    body = 'See all listings for "Homes in San Juan de Alicante, Alicante"'
    assert extract_search_name(subject, body) == "Homes in San Juan de Alicante"


def test_resolve_profile_creates_by_search_name(app):
    with app.app_context():
        profile = SearchProfileService.resolve_profile(
            "New detached house in your search: Search Junio!",
            "See all listings for 'Search Junio'",
        )
        assert profile is not None
        assert profile.name == "Junio"

        # Second call should return the same profile (no duplicates).
        profile2 = SearchProfileService.resolve_profile(
            "Price reduction in your search: Search Junio!",
            "",
        )
        assert profile2 is not None
        assert profile2.id == profile.id


def test_resolve_profile_creates_by_search_name_without_search_prefix(app):
    with app.app_context():
        profile = SearchProfileService.resolve_profile(
            'New detached house in your search: "Homes in Ciudad Quesada"',
            'See all listings for "Homes in Ciudad Quesada"',
        )
        assert profile is not None
        assert profile.name == "Homes in Ciudad Quesada"


def test_resolve_profile_creates_by_search_name_from_subject_without_quotes(app):
    with app.app_context():
        profile = SearchProfileService.resolve_profile(
            "New home in your search: Homes in San Juan de Alicante, Alicante!",
            'See all listings for "Homes in San Juan de Alicante, Alicante"',
        )
        assert profile is not None
        assert profile.name == "Homes in San Juan de Alicante"


def test_travel_preset_defs_use_train_station_not_subway():
    defs = SearchProfileService.get_travel_preset_defs()
    keys = {d["key"] for d in defs}

    assert "train_station" in keys
    assert "subway_station" not in keys


def test_normalize_drops_legacy_subway_preset():
    cfg = normalize_travel_targets_config(
        {
            "presets": {
                "airport": {"enabled": True, "mode": "driving"},
                "train_station": {"enabled": True, "mode": "driving"},
                "subway_station": {"enabled": True, "mode": "transit"},
            },
            "custom": [],
        }
    )

    presets = cfg.get("presets") or {}
    assert "train_station" in presets
    assert "subway_station" not in presets
