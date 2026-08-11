"""Issue #260: the beach list may not buy a second Distance Matrix request.

`services/property_travel_service.py` promises, twice in comments and once in
CLAUDE.md, that the beach lookup "rides along in the same Distance Matrix batch
as the presets, so it costs one extra Places call per property and no extra
Distance Matrix request".

Six presets plus twenty beach candidates is 26 destinations in one `driving`
group, against `_MAX_DESTINATIONS_PER_REQUEST = 25` — `_get_distances` splits
that into two billed calls, on exactly the coastal listings the beach list is
for. The existing beach suite cannot see it: it patches `_get_distances` away
and stubs the presets to return nothing, so the merged-then-chunked path is
never driven.

This drives the real merge and counts the batches.
"""

from unittest.mock import patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import property_travel_service as module
from services.property_travel_service import (
    _MAX_DESTINATIONS_PER_REQUEST,
    PropertyTravelService,
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


@pytest.fixture
def coastal_property(app):
    """A listing under a profile with every preset enabled, all driving."""
    profile = SearchProfile(
        name="Coast",
        is_active=True,
        is_default=True,
        travel_targets={
            "presets": {
                name: {"enabled": True, "mode": "driving"}
                for name in (
                    "airport",
                    "train_station",
                    "hospital",
                    "police",
                    "supermarket",
                    "school",
                )
            },
            "custom": [],
        },
    )
    db.session.add(profile)
    db.session.commit()
    prop = Property(
        source_email_id="issue-260",
        title="House by the sea",
        property_category="housing",
        search_profile_id=profile.id,
        location_lat=43.55,
        location_lon=-6.83,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def _beaches(count):
    return module.BeachLookup(
        places=[
            {
                "place_id": f"beach-{index}",
                "name": f"Playa {index}",
                "lat": 43.56 + index / 1000,
                "lon": -6.84,
            }
            for index in range(count)
        ],
        total_found=count,
    )


class TestTheMergedGroupFitsOneRequest:
    def test_six_presets_and_twenty_beaches_are_measured_in_one_batch(
        self, app, coastal_property
    ):
        """26 destinations used to be chunked into two billed requests."""
        batches = []

        def record(self, lat, lon, destinations, mode):
            batches.append(len(destinations))
            return [
                module.DistanceResult(distance_m=1000, duration_s=300)
                for _ in destinations
            ]

        with (
            patch.object(
                PropertyTravelService,
                "_nearest_place_for_preset",
                side_effect=lambda *a, **k: module.PlaceLookup(
                    place={
                        "place_id": "p",
                        "name": "Somewhere",
                        "lat": 43.5,
                        "lon": -6.8,
                    }
                ),
            ),
            patch.object(
                PropertyTravelService,
                "_beach_candidates",
                return_value=_beaches(20),
            ),
            patch.object(PropertyTravelService, "_get_distances", record),
        ):
            PropertyTravelService().calculate_for_property(
                coastal_property, commit=False
            )

        assert batches, "no distance request was made at all"
        assert max(batches) <= _MAX_DESTINATIONS_PER_REQUEST, (
            f"a group of {max(batches)} destinations is billed as two requests"
        )

    def test_the_beaches_that_fit_are_still_measured(self, app, coastal_property):
        """Reserving room must not silently drop the whole block."""
        with (
            patch.object(
                PropertyTravelService,
                "_nearest_place_for_preset",
                side_effect=lambda *a, **k: module.PlaceLookup(
                    place={
                        "place_id": "p",
                        "name": "Somewhere",
                        "lat": 43.5,
                        "lon": -6.8,
                    }
                ),
            ),
            patch.object(
                PropertyTravelService,
                "_beach_candidates",
                return_value=_beaches(20),
            ),
            patch.object(
                PropertyTravelService,
                "_get_distances",
                lambda self, lat, lon, destinations, mode: [
                    module.DistanceResult(distance_m=1000, duration_s=300)
                    for _ in destinations
                ],
            ),
        ):
            PropertyTravelService().calculate_for_property(
                coastal_property, commit=False
            )

        beaches = (coastal_property.travel or {}).get("beaches") or {}
        assert beaches.get("items"), "the block vanished instead of being trimmed"

    def test_a_listing_with_few_presets_still_gets_every_beach(self, app):
        """The reservation only bites where the group would have overflowed."""
        profile = SearchProfile(
            name="Few presets",
            is_active=True,
            travel_targets={
                "presets": {"airport": {"enabled": True, "mode": "driving"}},
                "custom": [],
            },
        )
        db.session.add(profile)
        db.session.commit()
        prop = Property(
            source_email_id="issue-260-few",
            title="House by the sea",
            property_category="housing",
            search_profile_id=profile.id,
            location_lat=43.55,
            location_lon=-6.83,
        )
        db.session.add(prop)
        db.session.commit()

        measured = []

        def record(self, lat, lon, destinations, mode):
            measured.append(len(destinations))
            return [
                module.DistanceResult(distance_m=1000, duration_s=300)
                for _ in destinations
            ]

        with (
            patch.object(
                PropertyTravelService,
                "_nearest_place_for_preset",
                side_effect=lambda *a, **k: module.PlaceLookup(
                    place={
                        "place_id": "p",
                        "name": "Somewhere",
                        "lat": 43.5,
                        "lon": -6.8,
                    }
                ),
            ),
            patch.object(
                PropertyTravelService, "_beach_candidates", return_value=_beaches(20)
            ),
            patch.object(PropertyTravelService, "_get_distances", record),
        ):
            PropertyTravelService().calculate_for_property(prop, commit=False)

        assert max(measured) <= _MAX_DESTINATIONS_PER_REQUEST
        assert max(measured) >= 20, "every beach that fits must still be measured"
