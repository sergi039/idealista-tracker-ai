"""Municipality truncation: the email's ellipsis is a marker, not a name.

Idealista alert emails cut long location strings mid-name ("Ovi...",
"Mieres de...") and ingestion stored them verbatim, so the /properties
municipality filter offered the artifacts as municipalities of their own and
filtering by the full name silently missed the truncated rows (issue #298).

Three layers are pinned here, in the order the data flows:

* the pure resolution rule in `utils/idealista_extractors.py` -- a truncated
  stem resolves only to the *unique* stored full name that starts with it,
  and anything ambiguous or unmatched stays truncated, never guessed;
* the ingestion hook in `services/property_imap_service.py` that applies the
  rule against the municipalities already in the table;
* the filter options in `routes/main_routes.py`, which never offer a
  marker-suffixed value as a dropdown choice (while `_keep_applied_choice`
  still keeps one the URL explicitly applied);
* and the one-time repair tool `utils/resolve_truncated_municipalities.py`
  for the rows stored before the hook existed: snapshot first, per-row
  commit, idempotent.

The fixtures are the incident's own values, verified against the live table
on 2026-08-14: "Mieres de..." resolving to "Mieres Del Camino" is why the
match is casefolded, and "Corredoria-La Carisa-Prado de L..." is why an
unmatched stem must survive verbatim.
"""

import json

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from services.property_imap_service import PropertyIMAPService  # noqa: E402
from utils import resolve_truncated_municipalities as tool  # noqa: E402
from utils.idealista_extractors import (  # noqa: E402
    extract_municipality_from_title,
    is_truncated_municipality,
    municipality_truncation_stem,
    resolve_truncated_municipality,
)


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


def _property(key, municipality, profile_id=None):
    prop = Property(
        source_email_id=f"truncation-{key}",
        title=f"Listing {key}",
        municipality=municipality,
        search_profile_id=profile_id,
        listing_status="active",
    )
    db.session.add(prop)
    db.session.commit()
    return prop


class TestDetection:
    def test_both_marker_forms_are_truncation(self):
        assert is_truncated_municipality("Ovi...")
        assert is_truncated_municipality("Ovi…")
        assert is_truncated_municipality("Mieres de... ")

    def test_a_full_name_is_not(self):
        assert not is_truncated_municipality("Oviedo")
        assert not is_truncated_municipality(None)
        assert not is_truncated_municipality("")
        # A single trailing dot is punctuation, not the email's marker.
        assert not is_truncated_municipality("S. Martín del Rey A.")

    def test_the_stem_drops_the_marker_and_its_whitespace(self):
        assert municipality_truncation_stem("Ovi...") == "Ovi"
        assert municipality_truncation_stem("Mieres de …") == "Mieres de"
        assert municipality_truncation_stem("Oviedo") is None
        # A marker with nothing in front of it offers no stem at all.
        assert municipality_truncation_stem("...") is None


class TestResolution:
    KNOWN = [
        "Oviedo",
        "Mieres Del Camino",
        "La Pedrera - Leorio - Huerces - Ruedes",
        "San Martín del Rey Aurelio",
        "San Esteban",
        "Rojales",
        "Gijón",
    ]

    def test_a_unique_prefix_resolves_to_the_full_name(self):
        assert resolve_truncated_municipality("Ovi...", self.KNOWN) == "Oviedo"
        assert resolve_truncated_municipality("Ovied...", self.KNOWN) == "Oviedo"
        assert resolve_truncated_municipality("Rojal...", self.KNOWN) == "Rojales"
        assert (
            resolve_truncated_municipality(
                "La Pedrera - Leorio - Huerces - Ru...", self.KNOWN
            )
            == "La Pedrera - Leorio - Huerces - Ruedes"
        )

    def test_matching_is_casefolded(self):
        """The incident's own row: 'Mieres de...' vs the stored 'Mieres Del'."""
        assert (
            resolve_truncated_municipality("Mieres de...", self.KNOWN)
            == "Mieres Del Camino"
        )

    def test_an_ambiguous_stem_is_never_guessed(self):
        # "San..." could be San Martín del Rey Aurelio or San Esteban.
        assert resolve_truncated_municipality("San...", self.KNOWN) is None

    def test_an_unmatched_stem_is_never_guessed(self):
        assert (
            resolve_truncated_municipality(
                "Corredoria-La Carisa-Prado de L...", self.KNOWN
            )
            is None
        )

    def test_a_stem_too_short_to_prove_anything_is_refused(self):
        # "Oviedo" is the only O-name here, but two characters matching
        # uniquely is luck, not evidence.
        assert resolve_truncated_municipality("Ov...", self.KNOWN) is None

    def test_a_truncated_known_value_is_never_a_resolution_target(self):
        assert resolve_truncated_municipality("Ovi...", ["Ovied...", "Ovi..."]) is None
        # And it does not spoil uniqueness when the full name is there too.
        assert (
            resolve_truncated_municipality("Ovi...", ["Ovied...", "Oviedo"]) == "Oviedo"
        )

    def test_casing_variants_of_one_name_still_count_as_unique(self):
        """The table really holds 'Corvera De Asturias' and 'Corvera de
        Asturias' side by side; they are one municipality, not an ambiguity.
        The surface form is picked deterministically."""
        known = ["Corvera De Asturias", "Corvera de Asturias"]
        assert (
            resolve_truncated_municipality("Corvera De Astur...", known)
            == "Corvera De Asturias"
        )

    def test_a_full_value_passes_through_unresolved(self):
        assert resolve_truncated_municipality("Oviedo", self.KNOWN) is None
        assert resolve_truncated_municipality(None, self.KNOWN) is None


class TestParserKeepsTheMarker:
    def test_the_extractor_reports_what_the_email_said(self):
        """Resolution needs the database, so the pure extractor must hand the
        marker onwards rather than silently strip it into a fake full name."""
        title = "Land in Calle Uría, Ovi... 85,000 €"
        assert extract_municipality_from_title(title) == "Ovi..."


class TestIngestionResolution:
    def test_a_unique_match_against_stored_rows_resolves(self, app):
        _property("full", "Oviedo")
        assert (
            PropertyIMAPService._resolve_municipality_truncation("Ovi...") == "Oviedo"
        )

    def test_an_ambiguous_match_keeps_the_marker(self, app):
        _property("full-a", "San Esteban")
        _property("full-b", "San Sadurniño")
        assert (
            PropertyIMAPService._resolve_municipality_truncation("San...") == "San..."
        )

    def test_an_unmatched_stem_keeps_the_marker(self, app):
        _property("full", "Oviedo")
        assert (
            PropertyIMAPService._resolve_municipality_truncation(
                "Parroquias suroccidentales..."
            )
            == "Parroquias suroccidentales..."
        )

    def test_untruncated_values_pass_through_untouched(self, app):
        _property("full", "Oviedo")
        assert PropertyIMAPService._resolve_municipality_truncation("Gijón") == "Gijón"
        assert PropertyIMAPService._resolve_municipality_truncation(None) is None


class TestFilterOptions:
    @pytest.fixture
    def seeded(self, app):
        profile = SearchProfile(
            name="Asturias",
            is_active=True,
            is_default=True,
            travel_targets={"presets": {}, "custom": []},
        )
        db.session.add(profile)
        db.session.commit()
        _property("oviedo", "Oviedo", profile.id)
        _property("ovi", "Ovi...", profile.id)
        _property("parroquias", "Parroquias suroccidentales...", profile.id)
        return profile

    def test_truncated_values_are_not_offered_as_choices(self, client, seeded):
        resp = client.get("/properties?profile_id=all")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'value="Oviedo"' in body
        assert 'value="Ovi..."' not in body
        assert 'value="Parroquias suroccidentales..."' not in body

    def test_an_applied_truncated_filter_still_shows_in_its_dropdown(
        self, client, seeded
    ):
        """`_keep_applied_choice`: the control must agree with the query it
        produced, even for a hand-typed truncated value."""
        resp = client.get(
            "/properties",
            query_string={"profile_id": "all", "municipality": "Ovi..."},
        )
        assert resp.status_code == 200
        assert 'value="Ovi..."' in resp.get_data(as_text=True)


class TestRepairTool:
    def _seed_the_incident(self):
        """The nine distinct values the live table held on 2026-08-14."""
        for key, value in [
            ("oviedo", "Oviedo"),
            ("mieres", "Mieres Del Camino"),
            ("pedrera", "La Pedrera - Leorio - Huerces - Ruedes"),
            ("aurelio", "San Martín del Rey Aurelio"),
            ("rojales", "Rojales"),
            ("t-ovi-1", "Ovi..."),
            ("t-ovi-2", "Ovi..."),
            ("t-ovied", "Ovied..."),
            ("t-mieres", "Mieres de..."),
            ("t-pedrera-1", "La Pedrera - Leorio - Huerces - Ru..."),
            ("t-pedrera-2", "La Pedrera - Leorio - Huerces - Rue..."),
            ("t-rojal", "Rojal..."),
            ("t-aureli", "San Martín del Rey Aureli..."),
            ("t-corredoria", "Corredoria-La Carisa-Prado de L..."),
            ("t-parroquias", "Parroquias suroccidentales..."),
        ]:
            _property(key, value)
        return Property.query.order_by(Property.id.asc()).all()

    def test_the_plan_resolves_the_incident_rows_it_can_prove(self, app):
        properties = self._seed_the_incident()
        plan = tool.plan_changes(properties)

        by_value = {}
        for prop, full in plan:
            by_value.setdefault(prop.municipality, set()).add(full)

        assert by_value == {
            "Ovi...": {"Oviedo"},
            "Ovied...": {"Oviedo"},
            "Mieres de...": {"Mieres Del Camino"},
            "La Pedrera - Leorio - Huerces - Ru...": {
                "La Pedrera - Leorio - Huerces - Ruedes"
            },
            "La Pedrera - Leorio - Huerces - Rue...": {
                "La Pedrera - Leorio - Huerces - Ruedes"
            },
            "Rojal...": {"Rojales"},
            "San Martín del Rey Aureli...": {"San Martín del Rey Aurelio"},
            # No stored full name starts with these stems: kept verbatim.
            "Corredoria-La Carisa-Prado de L...": {None},
            "Parroquias suroccidentales...": {None},
        }

    def test_apply_writes_only_the_resolvable_rows(self, app):
        properties = self._seed_the_incident()
        resolved, failed = tool.apply_plan(tool.plan_changes(properties))

        assert (resolved, failed) == (8, 0)
        remaining = tool.truncated_rows(Property.query.all())
        assert sorted(prop.municipality for prop in remaining) == [
            "Corredoria-La Carisa-Prado de L...",
            "Parroquias suroccidentales...",
        ]
        # Every resolved row now carries the full name.
        assert Property.query.filter_by(municipality="Oviedo").count() == 4

    def test_a_second_run_has_nothing_to_write(self, app):
        """Idempotence: resolved rows leave the selection, unresolved ones
        resolve to nothing again."""
        properties = self._seed_the_incident()
        tool.apply_plan(tool.plan_changes(properties))

        second = tool.plan_changes(Property.query.all())
        assert [full for _, full in second] == [None, None]
        assert tool.apply_plan(second) == (0, 0)

    def test_the_snapshot_round_trips_the_values_it_overwrites(self, app, tmp_path):
        properties = self._seed_the_incident()
        plan = tool.plan_changes(properties)
        resolvable = [(prop, full) for prop, full in plan if full]

        path = str(tmp_path / "snap.json")
        tool._write_snapshot([tool._snapshot_row(prop) for prop, _ in resolvable], path)
        tool.apply_plan(plan)
        assert Property.query.filter_by(municipality="Ovi...").count() == 0

        assert tool._restore(path) == 8
        assert Property.query.filter_by(municipality="Ovi...").count() == 2
        assert Property.query.filter_by(municipality="Oviedo").count() == 1

    def test_it_refuses_to_overwrite_an_existing_rollback_point(self, app, tmp_path):
        path = str(tmp_path / "snap.json")
        tool._write_snapshot([{"id": 1, "municipality": "Ovi..."}], path)

        with pytest.raises(SystemExit):
            tool._write_snapshot([{"id": 2, "municipality": "x"}], path)

        with open(path, encoding="utf-8") as handle:
            assert json.load(handle) == [{"id": 1, "municipality": "Ovi..."}]
