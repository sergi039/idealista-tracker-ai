"""Who is selling: the badge, the filter and the two readings behind them.

The feature exists because the owner cannot tell a private seller from an
agency on the list, and most of the answer was already in the table: Idealista
names the kind of advert in the campaign that delivered the alert email, and
the stored URL keeps it. So the thing worth pinning is not "does a badge
render" but the three ways this class of feature goes wrong here:

* **an absence read as an answer.** A row nobody could establish must stay
  `unchecked` and must never quietly become `agency` because agencies are the
  common case (#98's shape, one column over from `listing_status`).
* **two readings drifting.** The badge reads Python and the dropdown counts
  read SQL, so `TestTheTwoReadingsAgree` runs one matrix through both. A
  filter that selects rows the badge does not mark is worse than no filter.
* **a guess wearing a measurement's clothes.** Only `professional` has ever
  been seen in fotocasa's publisher type, so an unrecognised value has to fall
  to `unknown` -- never to `owner`.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import advertiser
from tests import setup_test_environment


# Exactly as ingestion stores them: the email's language segment and the ten
# tracking parameters, one of which is the campaign that answers the question.
URL_OWNER = (
    "https://www.idealista.com/en/inmueble/91523456/"
    "?utm_medium=email&utm_campaign=express_newAd_sale_particular"
    "&utm_source=alerts-id"
)
URL_AGENCY = (
    "https://www.idealista.com/en/inmueble/109757819/"
    "?utm_medium=email&utm_campaign=express_priceDrop_sale_professional"
    "&utm_source=alerts-id"
)
# The hand-imported batches: a bare listing link, no campaign, nothing to read.
URL_SILENT = "https://www.idealista.com/inmueble/111485227/"
URL_FOTOCASA = "https://www.fotocasa.es/es/comprar/terreno/carreno/carreno/189962611/d"


def _prop(**kwargs):
    """A listing row. The profile is the fixture's active subscription, because
    a bare `/properties` shows the live subscriptions and an unassigned row
    would be absent from the page for a reason that has nothing to do with
    what is under test."""
    row = Property(
        source_email_id=kwargs.pop("source_email_id", None) or kwargs.get("url", "x"),
        title=kwargs.pop("title", "A plot"),
        deal_type="sale",
        property_category="land",
        search_profile_id=kwargs.pop("search_profile_id", 1),
    )
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


@pytest.fixture
def app():
    setup_test_environment()
    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        db.create_all()
        db.session.add(
            SearchProfile(
                name="Land at Norte",
                is_active=True,
                is_default=True,
                travel_targets={"presets": {}, "custom": []},
            )
        )
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


class TestTheAlertLinkAnswersForFree:
    """408 of 730 rows carry the answer in the URL they were born with."""

    def test_a_particular_campaign_is_an_owner(self):
        assert advertiser.from_alert_url(URL_OWNER) == advertiser.OWNER

    def test_a_professional_campaign_is_an_agency(self):
        assert advertiser.from_alert_url(URL_AGENCY) == advertiser.AGENCY

    def test_a_link_with_no_campaign_answers_nothing(self):
        """Not `agency`. An absent token is an absent measurement."""
        assert advertiser.from_alert_url(URL_SILENT) is None
        assert advertiser.from_alert_url(URL_FOTOCASA) is None
        assert advertiser.from_alert_url(None) is None

    def test_the_state_is_read_off_the_row(self, app):
        assert advertiser.read_verdict(_prop(url=URL_OWNER))["state"] == "owner"
        assert advertiser.read_verdict(_prop(url=URL_AGENCY))["state"] == "agency"

    def test_a_row_nobody_established_is_unchecked_and_says_why(self, app):
        verdict = advertiser.read_verdict(_prop(url=URL_SILENT))
        assert verdict["state"] == "unchecked"
        assert verdict["established"] is False
        # The note has to name the reason this particular row cannot be read,
        # or "not established" reads as "not got round to it yet".
        assert "Idealista refuses this machine" in verdict["note"]


class TestPrecedence:
    """Strength, not recency -- and a hand-set verdict outranks everything."""

    def test_a_page_reading_outranks_the_campaign_token(self, app):
        row = _prop(
            url=URL_OWNER,
            enrichment={"advertiser": {"state": "agency", "source": "portal_payload"}},
        )
        assert advertiser.read_verdict(row)["state"] == "agency"

    def test_the_campaign_token_outranks_a_page_that_said_nothing(self, app):
        """`unknown` is "the source did not say", which must not bury an answer
        the row is already holding."""
        row = _prop(
            url=URL_OWNER,
            enrichment={"advertiser": {"state": "unknown", "source": "portal_payload"}},
        )
        assert advertiser.read_verdict(row)["state"] == "owner"

    def test_a_hand_set_verdict_outranks_both(self, app):
        row = _prop(
            url=URL_AGENCY,
            enrichment={"advertiser": {"state": "owner", "source": "manual"}},
        )
        verdict = advertiser.read_verdict(row)
        assert verdict["state"] == "owner"
        assert verdict["source"] == "manual"

    def test_a_stored_state_nobody_recognises_is_ignored(self, app):
        row = _prop(
            url=URL_OWNER,
            enrichment={"advertiser": {"state": "vendor", "source": "portal_payload"}},
        )
        assert advertiser.read_verdict(row)["state"] == "owner"


class TestTheTwoReadingsAgree:
    """`read_verdict` and `state_expression` are one answer in two languages.

    The dropdown prints a count beside every option and the badges are drawn
    from the other reading; a disagreement is a third wrong number rather than
    a disclosure. This is the matrix `services/listing_verification.py` runs
    for the same reason, applied to this pair.
    """

    ROWS = [
        ("owner-by-alert", URL_OWNER, None, "owner"),
        ("agency-by-alert", URL_AGENCY, None, "agency"),
        ("silent-idealista", URL_SILENT, None, "unchecked"),
        ("fotocasa-unread", URL_FOTOCASA, None, "unchecked"),
        ("no-url", None, None, "unchecked"),
        (
            "owner-by-page",
            URL_FOTOCASA,
            {"advertiser": {"state": "owner", "source": "portal_payload"}},
            "owner",
        ),
        (
            "agency-by-page",
            URL_FOTOCASA,
            {"advertiser": {"state": "agency", "source": "portal_payload"}},
            "agency",
        ),
        (
            "page-said-nothing",
            URL_FOTOCASA,
            {"advertiser": {"state": "unknown", "source": "portal_payload"}},
            "unknown",
        ),
        (
            "hand-set-over-alert",
            URL_AGENCY,
            {"advertiser": {"state": "owner", "source": "manual"}},
            "owner",
        ),
        (
            "page-said-nothing-but-alert-did",
            URL_OWNER,
            {"advertiser": {"state": "unknown", "source": "portal_payload"}},
            "owner",
        ),
        ("other-enrichment-only", URL_SILENT, {"sea": {"status": "ok"}}, "unchecked"),
    ]

    def test_every_row_reads_the_same_both_ways(self, app):
        for name, url, enrichment, expected in self.ROWS:
            db.session.add(_prop(source_email_id=name, url=url, enrichment=enrichment))
        db.session.commit()

        in_sql = dict(
            db.session.query(
                Property.source_email_id, advertiser.state_expression(Property)
            ).all()
        )
        for name, _url, _enrichment, expected in self.ROWS:
            row = Property.query.filter_by(source_email_id=name).one()
            assert advertiser.read_verdict(row)["state"] == expected, name
            assert in_sql[name] == expected, name

    def test_an_underscore_in_the_token_is_not_a_wildcard(self, app):
        """`_` is a LIKE wildcard, and every campaign token is full of them.

        Without ESCAPE, `%_sale_particular%` matches `xsalexparticular` too --
        a URL shape nobody writes on purpose, but the same slip that made a
        pasted listing link match the wrong rows in `utils/listing_search.py`.
        The Python reading matches a literal substring and cannot drift; this
        pins the SQL half to it.
        """
        db.session.add(
            _prop(
                source_email_id="lookalike",
                url="https://www.example.com/xsalexparticular/1/",
            )
        )
        db.session.commit()
        row = Property.query.filter_by(source_email_id="lookalike").one()
        assert advertiser.read_verdict(row)["state"] == "unchecked"
        state = (
            db.session.query(advertiser.state_expression(Property))
            .filter(Property.source_email_id == "lookalike")
            .scalar()
        )
        assert state == "unchecked"

    def test_the_filter_selects_what_the_badge_marks(self, app):
        for name, url, enrichment, _expected in self.ROWS:
            db.session.add(_prop(source_email_id=name, url=url, enrichment=enrichment))
        db.session.commit()

        for state in advertiser.STATES:
            clause = advertiser.filter_clause(Property, state)
            selected = Property.query.filter(clause).all()
            assert selected, f"no rows for {state}"
            for row in selected:
                assert advertiser.read_verdict(row)["state"] == state

    def test_an_unknown_filter_value_is_not_a_filter(self, app):
        assert advertiser.filter_clause(Property, "") is None
        assert advertiser.filter_clause(Property, "landlord") is None


class TestThePortalReading:
    """Only the spelling that was measured decides."""

    def test_professional_is_an_agency(self):
        assert advertiser.from_portal_type("professional") == "agency"

    def test_particular_is_an_owner(self):
        """Measured, not assumed. The first production run read all 56 stored
        fotocasa listings on 2026-08-17: the portal served `professional` 46
        times and `particular` 10, and every `particular` carried a person's
        name as its client (`Carlos`, `Ángeles`, `Maria Eugenia`) against a
        company on every `professional`."""
        assert advertiser.from_portal_type("particular") == "owner"

    def test_an_unmeasured_spelling_is_unknown_and_never_owner(self):
        """The failure mode of a wrong guess must be "not established"."""
        for value in ("franchise", "", None, "PROFESIONAL"):
            assert advertiser.from_portal_type(value, client_type_id=7) == "unknown"

    def test_the_numeric_twin_never_decides(self):
        """`clientTypeId: 3` rode beside every `professional` measured, and
        nobody here knows what the other numbers mean."""
        assert advertiser.from_portal_type(None, client_type_id=3) == "unknown"

    def test_the_stored_block_keeps_what_the_page_said(self):
        block = advertiser.portal_verdict(
            portal_type="professional",
            client_type_id=3,
            client_name="ALDAMA INMOBILIARIA",
            site="fotocasa",
        )
        assert block["state"] == "agency"
        assert block["source"] == "portal_payload"
        assert block["evidence"]["publisher_type"] == "professional"
        assert block["evidence"]["client_type_id"] == 3
        assert block["evidence"]["client_name"] == "ALDAMA INMOBILIARIA"


class TestTheLookupSpendsNothingItNeedNot:
    """`enrich` fetches a page only when the row cannot answer for itself."""

    @staticmethod
    def _forbid_fetch(monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("a listing page was fetched")

        monkeypatch.setattr("services.fotocasa_source.fetch_listing", explode)

    def test_a_row_the_alert_answered_is_not_fetched(self, app, monkeypatch):
        self._forbid_fetch(monkeypatch)
        result = advertiser.enrich(_prop(url=URL_OWNER))
        assert result["refusal"] == advertiser.REFUSAL_ALREADY_KNOWN
        assert result["stored"] is False

    def test_a_hand_set_row_is_not_fetched(self, app, monkeypatch):
        self._forbid_fetch(monkeypatch)
        row = _prop(
            url=URL_FOTOCASA,
            enrichment={"advertiser": {"state": "unknown", "source": "manual"}},
        )
        assert advertiser.enrich(row)["refusal"] == advertiser.REFUSAL_HAND_SET

    def test_an_idealista_row_is_refused_without_a_request(self, app, monkeypatch):
        """Idealista answers a captcha to this machine, so there is no reader
        for it -- and a refusal that costs no request is the honest shape."""
        self._forbid_fetch(monkeypatch)
        result = advertiser.enrich(_prop(url=URL_SILENT))
        assert result["refusal"] == advertiser.REFUSAL_SITE_NOT_READABLE
        assert result["state"] == "unchecked"

    def test_a_fotocasa_page_is_read_and_stored(self, app, monkeypatch):
        from services import fotocasa_source

        listing = fotocasa_source.FotocasaListing(url=URL_FOTOCASA)
        listing.publisher_type = "professional"
        listing.client_type_id = 3
        listing.agency = "Astur Select Sl"
        monkeypatch.setattr(
            "services.fotocasa_source.fetch_listing", lambda url, **kw: listing
        )

        row = _prop(source_email_id="fc", url=URL_FOTOCASA)
        db.session.add(row)
        db.session.commit()

        result = advertiser.enrich(row, commit=True)
        assert result == {"state": "agency", "stored": True, "refusal": None}

        stored = Property.query.filter_by(source_email_id="fc").one()
        assert stored.enrichment["advertiser"]["state"] == "agency"
        assert advertiser.read_verdict(stored)["state"] == "agency"

    def test_a_refusal_writes_nothing_at_all(self, app, monkeypatch):
        from services import fotocasa_source

        refused = fotocasa_source.FotocasaListing(
            url=URL_FOTOCASA, refusal=fotocasa_source.REFUSAL_BLOCKED
        )
        monkeypatch.setattr(
            "services.fotocasa_source.fetch_listing", lambda url, **kw: refused
        )

        row = _prop(source_email_id="fc-refused", url=URL_FOTOCASA)
        db.session.add(row)
        db.session.commit()

        result = advertiser.enrich(row, commit=True)
        assert result["stored"] is False
        assert result["refusal"] == fotocasa_source.REFUSAL_BLOCKED
        # The state it *reports*, too, and not only the state it stores: the
        # backfill counts this value and an API caller would print it, so a
        # refusal answering "agency" would be #98 arriving through the return
        # value instead of through the column.
        assert result["state"] == "unchecked"

        stored = Property.query.filter_by(source_email_id="fc-refused").one()
        assert "advertiser" not in (stored.enrichment or {})
        assert advertiser.read_verdict(stored)["state"] == "unchecked"

    def test_a_verdict_written_while_the_page_was_being_fetched_survives(
        self, app, monkeypatch
    ):
        """The read-modify-write is decided under the lock, not before it.

        A fetch takes up to 20 s, and `enrichment` is one JSON column: whatever
        another process committed in that window is in the copy this session
        must re-read, or it is silently overwritten. That is #339, which cost
        two properties their measured pool data on 2026-08-16 -- and the guard
        that prevents it can only be exercised by writing during the fetch.
        """
        from services import fotocasa_source

        row = _prop(source_email_id="fc-raced", url=URL_FOTOCASA)
        db.session.add(row)
        db.session.commit()
        row_id = row.id

        def fetch_and_race(url, **kwargs):
            db.session.execute(
                db.text("UPDATE properties SET enrichment = :e WHERE id = :i"),
                {
                    "e": '{"advertiser": {"state": "owner", "source": "manual"}}',
                    "i": row_id,
                },
            )
            db.session.commit()
            listing = fotocasa_source.FotocasaListing(url=url)
            listing.publisher_type = "professional"
            listing.client_type_id = 3
            return listing

        monkeypatch.setattr("services.fotocasa_source.fetch_listing", fetch_and_race)

        result = advertiser.enrich(row, commit=True)
        assert result["stored"] is False

        stored = Property.query.filter_by(source_email_id="fc-raced").one()
        assert stored.enrichment["advertiser"]["source"] == "manual"
        assert advertiser.read_verdict(stored)["state"] == "owner"

    def test_a_refusal_arms_the_breaker_the_status_checker_shares(
        self, app, monkeypatch
    ):
        """One host, one breaker. A fotocasa that is refusing the status button
        must not be asked by this one either."""
        from services import fotocasa_source
        from services.listing_status_service import ListingStatusService

        refused = fotocasa_source.FotocasaListing(
            url=URL_FOTOCASA, refusal=fotocasa_source.REFUSAL_BLOCKED
        )
        monkeypatch.setattr(
            "services.fotocasa_source.fetch_listing", lambda url, **kw: refused
        )
        row = _prop(url=URL_FOTOCASA)
        for _ in range(3):
            advertiser.enrich(row)

        assert ListingStatusService.breakers.for_url(URL_FOTOCASA).state()["open"]
        # And now the fetch is not even attempted.
        self._forbid_fetch(monkeypatch)
        assert advertiser.enrich(row)["refusal"] == advertiser.REFUSAL_BACKING_OFF


class TestTheListSaysIt:
    """The badge, on the page, for the state it is about -- and only that one.

    Every test asserts the page really rendered: `routes/main_routes.py` turns
    a template error into a flash and a second render with no rows, which shows
    no badge either, so "the badge is absent" is exactly what a broken page
    looks like.
    """

    @staticmethod
    def _rendered_body(response):
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "An error occurred while loading properties" not in body
        return body

    def _seed(self):
        db.session.add_all(
            [
                _prop(source_email_id="o", title="Owner plot", url=URL_OWNER),
                _prop(source_email_id="a", title="Agency plot", url=URL_AGENCY),
                _prop(source_email_id="s", title="Silent plot", url=URL_SILENT),
            ]
        )
        db.session.commit()

    def test_an_owner_listing_is_badged(self, app, client):
        self._seed()
        body = self._rendered_body(client.get("/properties?profile_id=all"))
        assert "Private owner" in body

    def test_nothing_else_is_badged_as_an_owner(self, app, client):
        """An absence of knowledge is not a claim that a professional sells it,
        and it is not a claim that an owner does either."""
        db.session.add_all(
            [
                _prop(source_email_id="a", title="Agency plot", url=URL_AGENCY),
                _prop(source_email_id="s", title="Silent plot", url=URL_SILENT),
            ]
        )
        db.session.commit()
        body = self._rendered_body(client.get("/properties?profile_id=all"))
        assert "Private owner" not in body

    def test_the_seller_dropdown_counts_every_state(self, app, client):
        """Including the rows nobody could answer for: the list badges only
        `owner`, so this dropdown is the disclosure."""
        self._seed()
        body = self._rendered_body(client.get("/properties?profile_id=all"))
        assert 'name="advertiser"' in body
        assert "Private owner (1)" in body
        assert "Agency (1)" in body
        assert "Not established (1)" in body

    def test_the_filter_narrows_the_list(self, app, client):
        self._seed()
        body = self._rendered_body(
            client.get("/properties?profile_id=all&advertiser=owner")
        )
        assert "Owner plot" in body
        assert "Agency plot" not in body
        assert "Silent plot" not in body

    def test_the_json_payload_carries_the_verdict(self, app):
        """`enrichment` alone answers for none of the 408 free rows: the
        verdict is derived, so a consumer reading the column would see an
        empty block and conclude nothing was known."""
        row = _prop(url=URL_OWNER)
        assert row.to_dict()["advertiser_verdict"] == "owner"
        assert _prop(url=URL_SILENT).to_dict()["advertiser_verdict"] == "unchecked"

    def test_the_export_carries_the_verdict_and_its_source(self, app, client):
        self._seed()
        response = client.get("/properties/export.csv?profile_id=all")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "Advertiser,Advertiser Source" in body
        assert "owner,alert_campaign" in body
        assert "unchecked," in body
