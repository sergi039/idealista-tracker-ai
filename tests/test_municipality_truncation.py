"""Municipality truncation: the email's ellipsis is a marker, not a name.

Idealista alert emails cut long location strings mid-name ("Ovi...",
"Mieres de...") and ingestion stored them verbatim, so the /properties
municipality filter offered the artifacts as municipalities of their own and
filtering by the full name silently missed the truncated rows (issue #298).
Municipality is not just the dropdown: it feeds the /municipalities
comparison and its INE join too, so a truncated artifact was also a
comparison row whose facts could never match.

The layers pinned here, in the order the data flows:

* the pure resolution rule in `utils/idealista_extractors.py` -- a truncated
  stem resolves only to the *unique* stored full name that starts with it,
  anything ambiguous or unmatched stays truncated, and a stem cut at a bare
  connective (de/del/la/...) is refused outright: the "known" universe is
  only what ingestion has seen, so "San Juan de..." for the never-stored
  San Juan de la Arena would confidently pick San Juan de Alicante;
* the ingestion hook in `services/property_imap_service.py`, exercised both
  at the helper and through the real fetch/parse/ingest path with only
  `IMAPClient` faked;
* the /properties filter options in `routes/main_routes.py`, which never
  offer a marker-suffixed value as a dropdown choice;
* the /municipalities comparison, which skips truncated artifacts and counts
  them into the page's unnamed-listings footnote;
* and the repair tool `utils/resolve_truncated_municipalities.py`: plan
  buckets (auto / mapped / needs mapping / unmatched), operator `--map` for
  the connective rows, durable snapshot, loud restore, idempotence.

The fixtures are the incident's own values, verified against the live table
on 2026-08-14.
"""

import json
import os
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from tests import setup_test_environment

setup_test_environment()

from app import create_app, db  # noqa: E402
from config import Config  # noqa: E402
from models import Property, SearchProfile  # noqa: E402
from services import quality_of_life_service as qol_module  # noqa: E402
from services.municipality_comparison_service import (  # noqa: E402
    MunicipalityComparisonService,
)
from services.property_imap_service import PropertyIMAPService  # noqa: E402
from utils import resolve_truncated_municipalities as tool  # noqa: E402
from utils.idealista_extractors import (  # noqa: E402
    extract_municipality_from_title,
    is_truncated_municipality,
    municipality_truncation_stem,
    resolve_truncated_municipality,
    truncation_stem_ends_at_connective,
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
        """The email says 'del', the stored row says 'Del'."""
        assert (
            resolve_truncated_municipality("Mieres del Cam...", self.KNOWN)
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


class TestConnectiveStemRefusal:
    """A stem cut at a bare connective is refused even when it matches
    uniquely: the stored names are not Spain, so "exactly one sibling starts
    with it" proves only which sibling ingestion happens to know."""

    def test_the_verified_wrong_pick_scenarios_are_refused(self):
        # San Juan de la Arena exists and was never stored: a unique match
        # here would confidently record the wrong region.
        assert (
            resolve_truncated_municipality("San Juan de...", ["San Juan de Alicante"])
            is None
        )
        assert resolve_truncated_municipality("Soto de...", ["Soto Del Barco"]) is None
        assert (
            resolve_truncated_municipality(
                "San Martin de...", ["San Martín del Rey Aurelio"]
            )
            is None
        )
        # The incident's own connective row waits for an explicit mapping.
        assert (
            resolve_truncated_municipality("Mieres de...", ["Mieres Del Camino"])
            is None
        )

    def test_the_connective_shape_is_detected(self):
        assert truncation_stem_ends_at_connective("San Juan de...")
        assert truncation_stem_ends_at_connective("Mieres de...")
        assert truncation_stem_ends_at_connective("Soto del…")

    def test_a_distinctive_last_word_is_not_the_connective_shape(self):
        assert not truncation_stem_ends_at_connective("Ovi...")
        assert not truncation_stem_ends_at_connective("Mieres del Cam...")
        assert not truncation_stem_ends_at_connective(
            "La Pedrera - Leorio - Huerces - Ru..."
        )
        # A connective inside the stem is fine; only the cut point matters.
        assert not truncation_stem_ends_at_connective("San Martín del Rey Aureli...")
        assert not truncation_stem_ends_at_connective("Oviedo")


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

    def test_a_connective_stem_keeps_the_marker_and_warns(self, app, caplog):
        _property("full", "San Juan de Alicante")
        with caplog.at_level("WARNING", logger="services.property_imap_service"):
            kept = PropertyIMAPService._resolve_municipality_truncation(
                "San Juan de..."
            )
        assert kept == "San Juan de..."
        assert any("generic connective" in message for message in caplog.messages)

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


INTERNAL_DATE = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


class _FakeIMAPClient:
    """Stands in for the IMAP server only; everything below it is real code."""

    payloads: dict[int, bytes] = {}

    def __init__(self, host, port=None, ssl=None, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, user, password):
        return True

    def select_folder(self, name, readonly=True):
        return None

    def search(self, args):
        return sorted(_FakeIMAPClient.payloads)

    def fetch(self, uids, parts):
        return {
            uid: {
                b"RFC822": _FakeIMAPClient.payloads[uid],
                b"INTERNALDATE": INTERNAL_DATE,
            }
            for uid in uids
        }


def _raw_email(title: str, url: str) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "1 new listing that matches your search criteria"
    msg["From"] = "Idealista <noresponder@idealista.com>"
    msg.set_content("See it in your browser")
    msg.add_alternative(
        f'<html><body><a href="{url}">{title}</a><p>250,000 €</p></body></html>',
        subtype="html",
    )
    return msg.as_bytes()


@pytest.fixture
def uid_file(tmp_path, monkeypatch):
    """Keep the UID cursor inside the test's tmp dir, never the repo/data one."""
    path = tmp_path / ".last_seen_uid_properties"
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PROPERTIES_PATH", str(path))
    monkeypatch.setattr(Config, "LAST_SEEN_UID_PATH", str(tmp_path / ".last_seen_uid"))
    monkeypatch.setattr(Config, "BASE_DIR", str(tmp_path / "base"))
    return path


def _ingest(monkeypatch):
    monkeypatch.setattr(
        "services.property_imap_service.IMAPClient", _FakeIMAPClient, raising=True
    )
    monkeypatch.setattr(Config, "AUTO_TRAVEL_ENRICHMENT", False)
    monkeypatch.setattr(Config, "AUTO_PROPERTY_SCORING", False)
    monkeypatch.setattr(Config, "SEA_DISTANCE_ENABLED", False)
    service = PropertyIMAPService()
    service.user = "owner@example.com"
    service.password = "dummy"
    service.host = "imap.example.com"
    service.folder = "Idealista"
    return service.run_ingestion(sync_type="test")


class TestIngestionPipeline:
    """The real fetch/parse/ingest path, with only IMAPClient faked."""

    def test_a_safe_truncation_is_stored_under_the_full_name(
        self, app, uid_file, monkeypatch
    ):
        _property("stored-full", "Oviedo")
        _FakeIMAPClient.payloads = {
            1: _raw_email(
                "Detached house in Calle Uría, 12, Ovi...",
                "https://www.idealista.com/inmueble/990298/",
            )
        }

        assert _ingest(monkeypatch) == 1
        prop = Property.query.filter_by(idealista_property_id=990298).one()
        assert prop.municipality == "Oviedo"

    def test_a_connective_truncation_is_stored_verbatim(
        self, app, uid_file, monkeypatch
    ):
        # The unique sibling is stored, and it must NOT be picked: the email
        # could equally be the never-stored San Juan de la Arena.
        _property("stored-full", "San Juan de Alicante")
        _FakeIMAPClient.payloads = {
            1: _raw_email(
                "Detached house in Calle Mayor, 4, San Juan de...",
                "https://www.idealista.com/inmueble/990299/",
            )
        }

        assert _ingest(monkeypatch) == 1
        prop = Property.query.filter_by(idealista_property_id=990299).one()
        assert prop.municipality == "San Juan de..."


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


class TestMunicipalitiesComparison:
    @pytest.fixture
    def no_reference_files(self, tmp_path, monkeypatch):
        """Point the QoL reference loaders at an empty dir: hermetic, and the
        service already answers "not matched" for missing files."""
        for attr in ("INE_DATA_PATH", "CNH_DATA_PATH", "SEPE_DATA_PATH"):
            monkeypatch.setattr(qol_module, attr, str(tmp_path / f"{attr}.json"))
        return tmp_path

    def test_build_rows_never_buckets_a_truncated_artifact(
        self, app, no_reference_files
    ):
        _property("navia", "Navia")
        _property("ovi", "Ovi...")
        rows = MunicipalityComparisonService().build_rows(Property.query.all())
        assert [row["name"] for row in rows] == ["Navia"]

    def test_the_page_counts_truncated_rows_into_the_unnamed_footnote(
        self, client, app, no_reference_files
    ):
        _property("navia", "Navia")
        _property("ovi", "Ovi...")
        _property("blank", None)
        resp = client.get("/municipalities")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "Ovi..." not in body
        # One truncated + one empty municipality: both in the footnote.
        assert "2 listings carry no municipality" in body


class TestRepairToolPlan:
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

    MIERES_MAPPING = {"Mieres de...": "Mieres Del Camino"}

    def test_the_plan_buckets_the_incident_rows_honestly(self, app):
        properties = self._seed_the_incident()
        plan = tool.plan_changes(properties)

        by_value = {}
        for entry in plan:
            by_value.setdefault(entry.prop.municipality, set()).add(
                (entry.full, entry.kind)
            )

        assert by_value == {
            "Ovi...": {("Oviedo", tool.KIND_AUTO)},
            "Ovied...": {("Oviedo", tool.KIND_AUTO)},
            "La Pedrera - Leorio - Huerces - Ru...": {
                ("La Pedrera - Leorio - Huerces - Ruedes", tool.KIND_AUTO)
            },
            "La Pedrera - Leorio - Huerces - Rue...": {
                ("La Pedrera - Leorio - Huerces - Ruedes", tool.KIND_AUTO)
            },
            "Rojal...": {("Rojales", tool.KIND_AUTO)},
            "San Martín del Rey Aureli...": {
                ("San Martín del Rey Aurelio", tool.KIND_AUTO)
            },
            # The connective stem is never auto-resolved, even though
            # "Mieres Del Camino" matches uniquely: it waits for --map.
            "Mieres de...": {(None, tool.KIND_NEEDS_MAPPING)},
            # No stored full name starts with these stems: kept verbatim.
            "Corredoria-La Carisa-Prado de L...": {(None, tool.KIND_UNMATCHED)},
            "Parroquias suroccidentales...": {(None, tool.KIND_UNMATCHED)},
        }

    def test_an_operator_mapping_covers_the_connective_row(self, app):
        properties = self._seed_the_incident()
        plan = tool.plan_changes(properties, self.MIERES_MAPPING)

        mieres = [e for e in plan if e.prop.municipality == "Mieres de..."]
        assert [(e.full, e.kind) for e in mieres] == [
            ("Mieres Del Camino", tool.KIND_MAPPED)
        ]

    def test_apply_writes_only_the_planned_rows(self, app):
        properties = self._seed_the_incident()
        resolved, failed = tool.apply_plan(tool.plan_changes(properties))

        assert (resolved, failed) == (7, 0)
        remaining = tool.truncated_rows(Property.query.all())
        assert sorted(prop.municipality for prop in remaining) == [
            "Corredoria-La Carisa-Prado de L...",
            "Mieres de...",
            "Parroquias suroccidentales...",
        ]
        # Every resolved row now carries the full name.
        assert Property.query.filter_by(municipality="Oviedo").count() == 4

    def test_apply_with_the_mapping_repairs_all_eight(self, app):
        properties = self._seed_the_incident()
        plan = tool.plan_changes(properties, self.MIERES_MAPPING)

        assert tool.apply_plan(plan) == (8, 0)
        remaining = tool.truncated_rows(Property.query.all())
        assert sorted(prop.municipality for prop in remaining) == [
            "Corredoria-La Carisa-Prado de L...",
            "Parroquias suroccidentales...",
        ]
        assert Property.query.filter_by(municipality="Mieres Del Camino").count() == 2

    def test_a_second_run_has_nothing_to_write(self, app):
        """Idempotence: resolved rows leave the selection, unresolved ones
        resolve to nothing again."""
        properties = self._seed_the_incident()
        tool.apply_plan(tool.plan_changes(properties, self.MIERES_MAPPING))

        second = tool.plan_changes(Property.query.all(), self.MIERES_MAPPING)
        assert [entry.full for entry in second] == [None, None]
        assert tool.apply_plan(second) == (0, 0)


class TestRepairToolMappings:
    def test_a_mapping_must_be_truncated_to_full(self):
        assert tool.parse_mappings(["Mieres de...=Mieres Del Camino"]) == {
            "Mieres de...": "Mieres Del Camino"
        }
        with pytest.raises(SystemExit):
            tool.parse_mappings(["no-equals-sign"])
        with pytest.raises(SystemExit):
            tool.parse_mappings(["Oviedo=Oviedo City"])  # key not truncated
        with pytest.raises(SystemExit):
            tool.parse_mappings(["Ovi...=Ovied..."])  # value itself truncated
        with pytest.raises(SystemExit):
            tool.parse_mappings(["Ovi...="])  # empty full name


class TestRepairToolSnapshot:
    def _seed(self):
        _property("full", "Oviedo")
        ovi_1 = _property("t-ovi-1", "Ovi...")
        ovi_2 = _property("t-ovi-2", "Ovi...")
        return [ovi_1, ovi_2]

    def test_the_snapshot_round_trips_the_values_it_overwrites(self, app, tmp_path):
        self._seed()
        plan = tool.plan_changes(Property.query.all())
        resolvable = [entry for entry in plan if entry.full]

        path = str(tmp_path / "snap.json")
        tool._write_snapshot([tool._snapshot_row(e.prop) for e in resolvable], path)
        tool.apply_plan(plan)
        assert Property.query.filter_by(municipality="Ovi...").count() == 0

        assert tool._restore(path) == 2
        assert Property.query.filter_by(municipality="Ovi...").count() == 2
        assert Property.query.filter_by(municipality="Oviedo").count() == 1

    def test_the_snapshot_is_written_durably_with_no_temp_left_behind(
        self, app, tmp_path
    ):
        path = tmp_path / "snap.json"
        tool._write_snapshot([{"id": 1, "municipality": "Ovi..."}], str(path))

        assert json.loads(path.read_text(encoding="utf-8")) == [
            {"id": 1, "municipality": "Ovi..."}
        ]
        assert os.listdir(tmp_path) == ["snap.json"], "temp file must not survive"

    def test_it_refuses_to_overwrite_an_existing_rollback_point(self, app, tmp_path):
        path = str(tmp_path / "snap.json")
        tool._write_snapshot([{"id": 1, "municipality": "Ovi..."}], path)

        with pytest.raises(SystemExit):
            tool._write_snapshot([{"id": 2, "municipality": "x"}], path)

        # And the first one is still intact.
        with open(path, encoding="utf-8") as handle:
            assert json.load(handle) == [{"id": 1, "municipality": "Ovi..."}]

    def test_restore_fails_loudly_on_a_corrupt_snapshot(self, app, tmp_path):
        garbled = tmp_path / "garbled.json"
        garbled.write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit):
            tool._restore(str(garbled))

        wrong_shape = tmp_path / "wrong.json"
        wrong_shape.write_text('{"id": 1}', encoding="utf-8")
        with pytest.raises(SystemExit):
            tool._restore(str(wrong_shape))
