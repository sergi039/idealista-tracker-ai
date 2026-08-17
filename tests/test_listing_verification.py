"""An unchecked default is not a status (issue: listing_status never verified).

`listing_status` is `'active'` from the moment a listing is ingested. Nothing
verified that value, and until `services/listing_verification.py` existed every
surface drew it exactly like a status a check had confirmed: property 192, which
the advertiser withdrew on 08/05/2026, read as a live listing on `/properties`,
in the CSV export and in the JSON API. Measured 2026-08-15: 1 of 311 land rows
had ever been checked, so the page was making 310 claims it could not back.

What is pinned here:

* the verdict itself -- `active` only with a source behind it, `unchecked`
  otherwise, and a terminal status always shown;
* that the SQL predicate behind the page's coverage count agrees with the
  per-row verdict, row for row. A header reading "12 of 311 verified" over a
  table drawing 9 ticks would be a third wrong number rather than a disclosure;
* that `/properties`, `/properties/<id>`, `/lands/<id>`, the properties CSV
  export and both JSON payloads state it. Every one of them presented the
  default as a status before.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import create_app, db
from models import Land, Property, SearchProfile
from services.listing_verification import read_verdict, verified_expression
from tests import setup_test_environment

# (stored status, source, expected state, expected `verified`)
#
# The matrix both readings are run through. `email` on an active row cannot be
# produced by any writer -- idealista mails removal notices, never "still up" --
# but it is here because the rule has to answer for it either way, and the
# answer is that a removal mail is not evidence of a live listing.
VERDICT_MATRIX = [
    ("active", "ingest", "unchecked", False),
    ("active", None, "unchecked", False),
    ("active", "email", "unchecked", False),
    ("active", "check", "active", True),
    ("active", "manual", "active", True),
    (None, "ingest", "unchecked", False),
    (None, None, "unchecked", False),
    ("unknown", "manual", "unchecked", False),
    ("unknown", "check", "unchecked", False),
    ("removed", "ingest", "removed", True),
    ("removed", None, "removed", True),
    ("removed", "email", "removed", True),
    ("removed", "check", "removed", True),
    ("sold", "manual", "sold", True),
    ("sold", None, "sold", True),
]


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


def _profile():
    profile = SearchProfile(
        name="Land at Norte",
        is_active=True,
        is_default=True,
        travel_targets={"presets": {}, "custom": []},
    )
    db.session.add(profile)
    db.session.commit()
    return profile


def _property(key, **overrides):
    fields = {
        "source_email_id": f"verify-{key}",
        "title": f"Listing {key}",
        "url": f"https://www.idealista.com/inmueble/{abs(hash(key)) % 10**8}/",
        "municipality": "El Franco",
        "property_category": "land",
        "price": 99000,
        "area": 2600,
        "listing_status": "active",
        "listing_status_source": "ingest",
    }
    fields.update(overrides)
    prop = Property(**fields)
    db.session.add(prop)
    db.session.commit()
    return prop


class TestTheVerdict:
    @pytest.mark.parametrize("status,source,state,verified", VERDICT_MATRIX)
    def test_matrix(self, app, status, source, state, verified):
        with app.app_context():
            prop = _property(
                f"{status}-{source}",
                listing_status=status,
                listing_status_source=source,
            )
            verdict = read_verdict(prop)
            assert verdict["state"] == state
            assert verdict["verified"] is verified

    def test_the_ingest_default_is_never_reported_as_live(self, app):
        """The whole point: the value a row is born with makes no claim."""
        with app.app_context():
            prop = _property("fresh")
            assert prop.listing_status == "active"
            assert read_verdict(prop)["state"] == "unchecked"

    def test_a_check_carries_its_age(self, app):
        with app.app_context():
            checked = datetime.now(timezone.utc) - timedelta(days=3)
            prop = _property(
                "recent",
                listing_status_source="check",
                listing_last_checked=checked,
            )
            verdict = read_verdict(prop)
            assert verdict["age_days"] == 3
            assert verdict["stale"] is False

    def test_an_old_check_is_stale_but_still_verified(self, app):
        with app.app_context():
            prop = _property(
                "old",
                listing_status_source="check",
                listing_last_checked=datetime.now(timezone.utc) - timedelta(days=200),
            )
            verdict = read_verdict(prop)
            assert verdict["verified"] is True
            assert verdict["stale"] is True
            assert verdict["age_days"] == 200

    def test_a_naive_timestamp_is_read_as_utc(self, app):
        """Postgres hands these back without a tzinfo; the subtraction must not
        raise on precisely the rows that were actually checked."""
        with app.app_context():
            naive = datetime.utcnow() - timedelta(days=2)
            prop = _property(
                "naive", listing_status_source="check", listing_last_checked=naive
            )
            assert read_verdict(prop)["age_days"] == 2

    def test_a_hand_set_status_is_dated_by_nothing(self, app):
        """`manual` is a claim, not a reading. Dating it would credit a check
        that never ran -- the false confirmation of #136 by another route."""
        with app.app_context():
            prop = _property("byhand", listing_status_source="manual")
            verdict = read_verdict(prop)
            assert verdict["state"] == "active"
            assert verdict["checked_at"] is None
            assert verdict["age_days"] is None

    def test_it_reads_a_land_row_too(self, app):
        with app.app_context():
            land = Land(
                source_email_id="verify-land",
                title="Legacy land",
                listing_status="active",
                listing_status_source="ingest",
            )
            db.session.add(land)
            db.session.commit()
            assert read_verdict(land)["state"] == "unchecked"


class TestTheQueryAgreesWithTheRow:
    """One rule, two readings. They are pinned against each other because the
    page renders both at once, and a coverage count that disagrees with its own
    badges is worse than no coverage count."""

    def test_every_matrix_row_agrees(self, app):
        with app.app_context():
            expected_verified = set()
            for status, source, _state, verified in VERDICT_MATRIX:
                prop = _property(
                    f"sql-{status}-{source}",
                    listing_status=status,
                    listing_status_source=source,
                )
                if verified:
                    expected_verified.add(prop.id)

            by_sql = {
                row.id
                for row in Property.query.filter(verified_expression(Property)).all()
            }
            assert by_sql == expected_verified

            for prop in Property.query.all():
                assert read_verdict(prop)["verified"] is (prop.id in by_sql), (
                    f"row {prop.id} ({prop.listing_status}/"
                    f"{prop.listing_status_source}) is read differently by the "
                    "verdict and by the query"
                )


class TestThePropertiesList:
    def test_the_coverage_line_counts_what_the_total_counts(self, app, client):
        with app.app_context():
            profile = _profile()
            for key, source in [
                ("a", "ingest"),
                ("b", "ingest"),
                ("c", "check"),
            ]:
                _property(
                    key,
                    search_profile_id=profile.id,
                    listing_status_source=source,
                    listing_last_checked=datetime.now(timezone.utc)
                    if source == "check"
                    else None,
                )

        body = client.get("/properties").get_data(as_text=True)
        assert "listing-verification-coverage" in body
        assert "1 of 3 verified on the source site" in body

    def test_the_count_follows_the_filters(self, app, client):
        """It is counted over the filtered result, like the total beside it."""
        with app.app_context():
            profile = _profile()
            _property(
                "shown",
                search_profile_id=profile.id,
                municipality="Coaña",
                listing_status_source="check",
                listing_last_checked=datetime.now(timezone.utc),
            )
            _property(
                "filtered-out",
                search_profile_id=profile.id,
                municipality="Tapia",
                listing_status_source="check",
                listing_last_checked=datetime.now(timezone.utc),
            )
            _property(
                "unchecked",
                search_profile_id=profile.id,
                municipality="Coaña",
            )

        body = client.get("/properties?municipality=Coaña").get_data(as_text=True)
        assert "1 of 2 verified on the source site" in body

    def test_an_unchecked_row_claims_nothing(self, app, client):
        with app.app_context():
            profile = _profile()
            _property("plain", search_profile_id=profile.id)

        body = client.get("/properties").get_data(as_text=True)
        assert 'data-listing-status="unchecked"' in body
        assert 'data-listing-status="active"' not in body
        # The badge that would assert a live listing.
        assert "fa-circle-check" not in body

    def test_a_verified_row_says_so_and_carries_its_age(self, app, client):
        """The age rides in the tooltip, not in the cell: measured on the
        rendered page, three words in the Type column wrap to three lines and
        double the row height."""
        with app.app_context():
            profile = _profile()
            _property(
                "checked",
                search_profile_id=profile.id,
                listing_status_source="check",
                listing_last_checked=datetime.now(timezone.utc) - timedelta(days=5),
            )

        body = client.get("/properties").get_data(as_text=True)
        assert 'data-listing-status="active"' in body
        assert "fa-circle-check" in body
        assert 'title="Confirmed against Idealista — checked 5 d ago' in body

    def test_a_long_ago_check_keeps_the_badge_and_loses_the_green(self, app, client):
        """It verified something -- the listing was up when it was read -- so
        suppressing the badge would discard a real observation. But "confirmed
        in March" is not the same claim about today as "confirmed yesterday",
        and the list has room to say that in weight, not in words."""
        with app.app_context():
            profile = _profile()
            _property(
                "long-ago",
                search_profile_id=profile.id,
                listing_status_source="check",
                listing_last_checked=datetime.now(timezone.utc) - timedelta(days=140),
            )

        body = client.get("/properties").get_data(as_text=True)
        assert 'data-listing-status="active"' in body
        assert "fa-clock-rotate-left" in body
        assert "fa-circle-check" not in body
        assert "checked 140 d ago" in body

    def test_a_removed_row_still_says_removed(self, app, client):
        with app.app_context():
            profile = _profile()
            _property(
                "gone",
                search_profile_id=profile.id,
                listing_status="removed",
                listing_status_source="email",
            )

        body = client.get("/properties?hide_removed=off").get_data(as_text=True)
        assert 'data-listing-status="removed"' in body


class TestThePropertyPage:
    def test_an_unchecked_listing_is_labelled_on_the_page(self, app, client):
        with app.app_context():
            prop = _property("detail-unchecked")
            property_id = prop.id

        body = client.get(f"/properties/{property_id}").get_data(as_text=True)
        assert 'id="listing-unverified-badge"' in body
        assert "Unverified" in body
        assert "Checked: never" in body

    def test_a_verified_listing_is_not(self, app, client):
        with app.app_context():
            prop = _property(
                "detail-checked",
                listing_status_source="check",
                listing_last_checked=datetime.now(timezone.utc),
            )
            property_id = prop.id

        body = client.get(f"/properties/{property_id}").get_data(as_text=True)
        assert 'id="listing-unverified-badge"' not in body
        assert "Live" in body


class TestTheLegacyLandPage:
    """The archived surface reads the same rule, from the same module: it kept
    its own copy of the source map, and a rule in two places ships
    half-changed."""

    def test_an_unchecked_land_is_labelled_too(self, app, client):
        with app.app_context():
            land = Land(
                source_email_id="verify-land-page",
                title="Legacy land",
                listing_status="active",
                listing_status_source="ingest",
            )
            db.session.add(land)
            db.session.commit()
            land_id = land.id

        body = client.get(f"/lands/{land_id}").get_data(as_text=True)
        assert 'id="listing-unverified-badge"' in body


class TestTheExports:
    def test_the_csv_exports_the_verdict_not_the_default(self, app, client):
        """A report built off this file is what recommended a dead listing."""
        with app.app_context():
            profile = _profile()
            _property("csv-unchecked", search_profile_id=profile.id)

        body = client.get("/properties/export.csv").get_data(as_text=True)
        header, row = body.splitlines()[0], body.splitlines()[1]
        assert "Status Source" in header
        assert "Status Checked At" in header
        columns = row.split(",")
        assert "unchecked" in columns
        assert "active" not in columns

    def test_the_json_property_list_carries_it_too(self, app, client):
        """The compact payload has its own hand-written dict, so it does not
        inherit `to_dict`'s disclosure and has to make it itself."""
        with app.app_context():
            profile = _profile()
            _property("json", search_profile_id=profile.id)
            profile_id = profile.id

        payload = client.get(f"/api/properties?profile_id={profile_id}").get_json()
        row = payload["properties"][0]
        assert row["listing_status"] == "active"
        assert row["listing_status_verdict"] == "unchecked"

    def test_to_dict_carries_the_verdict_beside_the_raw_column(self, app):
        with app.app_context():
            prop = _property("api")
            payload = prop.to_dict()
            # The raw column is unchanged -- consumers reading provenance still
            # need it -- but it no longer travels alone.
            assert payload["listing_status"] == "active"
            assert payload["listing_status_verdict"] == "unchecked"
