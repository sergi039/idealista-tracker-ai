"""The owner settles a doubtful pair, same or different (#633, PR 2 of 3).

`set_link` is the one writer of `property_duplicate_link`; the rule reads the
rows as owner edges (`same` first, `different` forbidden). Nothing here merges
or deletes a listing. The PostgreSQL half (locks, constraints, migration 029)
is in tests/test_postgres_migrations.py.
"""

import pytest
from sqlalchemy import select

from app import create_app, db
from models import Property, PropertyDuplicateLink, SearchProfile
from services import property_duplicates as dup
from tests import setup_test_environment
from utils import report_duplicates

YAE_TITLE = "Chalet en venta en calle Fiobre, Bergondo"
FOTO_TITLE = "House for sale in calle Fiobre, Bergondo"


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile(app):
    row = SearchProfile(name="Galicia", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _add(profile, slug, **kw):
    prop = Property(
        source_email_id=kw.pop("source_email_id", f"t:{slug}"),
        search_profile_id=profile.id if profile is not None else None,
        title=kw.pop("title", f"Listing {slug}"),
        municipality=kw.pop("municipality", "Bergondo"),
        **kw,
    )
    db.session.add(prop)
    db.session.commit()
    return prop


def ide(profile, listing_id, **kw):
    return _add(
        profile,
        f"i{listing_id}",
        url=f"https://www.idealista.com/inmueble/{listing_id}/",
        idealista_property_id=listing_id,
        **kw,
    )


def yae(profile, listing_id, **kw):
    return _add(
        profile,
        f"yaencontre:{listing_id}",
        url=f"https://www.yaencontre.com/venta/casas/inmueble-46389-{listing_id}",
        source_email_id=f"yaencontre:{listing_id}",
        **kw,
    )


def foto(profile, listing_id, **kw):
    return _add(
        profile,
        f"f{listing_id}",
        url=f"https://www.fotocasa.es/es/comprar/vivienda/x/{listing_id}/d",
        **kw,
    )


def possible_pair(profile):
    """Two rows the rule can only call `possible`: three fields, no corroboration."""
    a = yae(profile, 1, price=200000, area=120)
    b = foto(profile, 2, price=200000, area=120)
    return a, b


def confirmed_pair(profile, listing_id=93111111):
    a = ide(profile, listing_id, price=100000)
    b = yae(profile, listing_id, price=90000)
    return a, b


def link_rows():
    return PropertyDuplicateLink.query.order_by(PropertyDuplicateLink.id).all()


class TestSetLink:
    def test_sets_updates_and_deletes_one_row(self, app, profile):
        a, b = possible_pair(profile)
        first = dup.set_link(a.id, b.id, verdict="same", note="  same facade ")
        assert first["changed"] is True
        (row,) = link_rows()
        assert (row.verdict, row.source, row.note) == ("same", "owner", "same facade")
        assert row.decided_at is not None

        again = dup.set_link(a.id, b.id, verdict="same", note="same facade")
        assert again["changed"] is False
        assert PropertyDuplicateLink.query.count() == 1

        dup.set_link(b.id, a.id, verdict="different")
        (row,) = link_rows()
        assert row.verdict == "different"
        assert row.note is None

        cleared = dup.set_link(a.id, b.id, verdict=None)
        assert cleared["changed"] is True
        assert PropertyDuplicateLink.query.count() == 0
        assert dup.set_link(a.id, b.id, verdict=None)["changed"] is False

    def test_the_pair_is_stored_low_then_high_whatever_the_argument_order(
        self, app, profile
    ):
        a, b = possible_pair(profile)
        low, high = sorted((a.id, b.id))
        dup.set_link(high, low, verdict="same")
        (row,) = link_rows()
        assert (row.property_low_id, row.property_high_id) == (low, high)

    def test_evidence_is_the_rules_explanation_at_decision_time(self, app, profile):
        a, b = possible_pair(profile)
        dup.set_link(a.id, b.id, verdict="same")
        (row,) = link_rows()
        assert row.evidence["tier"] == dup.POSSIBLE
        assert row.evidence["signals"]["price"]["status"] == dup.MET
        assert row.evidence["signals"]["area"] == {"status": dup.MET, "value": 120.0}
        assert "vetoed_by" not in row.evidence

    @pytest.mark.parametrize(
        "args, kwargs",
        [
            ((1, 1), {"verdict": "same"}),
            ((1, 999999), {"verdict": "same"}),
            ((0, 1), {"verdict": "same"}),
            ((True, 2), {"verdict": "same"}),
            (("1", 2), {"verdict": "same"}),
        ],
    )
    def test_refusals_are_typed_and_write_nothing(self, app, profile, args, kwargs):
        a, b = possible_pair(profile)
        if args[0] == 1 and args[1] == 1:
            args = (a.id, a.id)
        elif args[1] == 999999:
            args = (a.id, 999999)
        elif args[0] == 0:
            args = (0, b.id)
        with pytest.raises(dup.LinkError):
            dup.set_link(*args, **kwargs)
        assert PropertyDuplicateLink.query.count() == 0

    def test_unknown_verdict_source_and_long_note_are_refused(self, app, profile):
        a, b = possible_pair(profile)
        with pytest.raises(dup.LinkError):
            dup.set_link(a.id, b.id, verdict="maybe")
        with pytest.raises(dup.LinkError):
            dup.set_link(a.id, b.id, verdict="same", source="robot")
        with pytest.raises(dup.LinkError):
            dup.set_link(a.id, b.id, verdict="same", note="x" * 501)
        assert PropertyDuplicateLink.query.count() == 0

    def test_it_refuses_a_session_with_work_pending(self, app, profile):
        a, b = possible_pair(profile)
        a_id, b_id = a.id, b.id  # read first: an expired row would autoflush
        a.title = "edited and not committed"
        with pytest.raises(dup.LinkError):
            dup.set_link(a_id, b_id, verdict="same")
        db.session.rollback()

    def test_the_owner_outranks_a_cadastral_veto_and_the_evidence_says_so(
        self, app, profile
    ):
        a = ide(profile, 93111111, cadastral_reference="1234567VK1234A0001AB")
        b = yae(profile, 93111111, cadastral_reference="1234567VK1234A0002CD")
        assert dup.explain(a, b)["vetoed_by"] == dup.SOURCE_CADASTRE

        result = dup.set_link(a.id, b.id, verdict="same")
        assert result["vetoed_by"] == dup.SOURCE_CADASTRE
        (row,) = link_rows()
        assert row.verdict == "same"
        assert row.evidence["vetoed_by"] == dup.SOURCE_CADASTRE
        links = dup.load_links([a.id, b.id])
        (group,) = dup.groups_with_links([a, b], links)
        assert group.ids == (a.id, b.id)

    def test_deleting_a_property_removes_its_links(self, app, profile):
        a, b = possible_pair(profile)
        dup.set_link(a.id, b.id, verdict="same")
        db.session.execute(Property.__table__.delete().where(Property.id == a.id))
        db.session.commit()
        # SQLite enforces ON DELETE CASCADE only with the pragma; the
        # declaration is what is asserted here, PostgreSQL proves the effect.
        fks = {
            fk.parent.name: fk.ondelete
            for fk in PropertyDuplicateLink.__table__.foreign_keys
        }
        assert fks == {
            "property_low_id": "CASCADE",
            "property_high_id": "CASCADE",
        }


class TestReads:
    def test_links_for_names_both_ends(self, app, profile):
        a, b = possible_pair(profile)
        c = foto(profile, 3, price=1)
        dup.set_link(a.id, b.id, verdict="same")
        dup.set_link(c.id, a.id, verdict="different")
        assert {
            (link.property_low_id, link.property_high_id)
            for link in dup.links_for(a.id)
        } == {
            tuple(sorted((a.id, b.id))),
            tuple(sorted((a.id, c.id))),
        }
        assert [link.verdict for link in dup.links_for(b.id)] == ["same"]

    def test_load_links_returns_pairs_inside_the_id_set_only(self, app, profile):
        a, b = possible_pair(profile)
        c = foto(profile, 3, price=1)
        dup.set_link(a.id, b.id, verdict="same")
        dup.set_link(a.id, c.id, verdict="different")
        links = dup.load_links([a.id, b.id])
        assert links.same_pairs == {tuple(sorted((a.id, b.id)))}
        assert links.different_pairs == frozenset()
        assert len(links) == 1
        assert len(dup.load_links([])) == 0


class TestLinksEnterTheRule:
    def test_owner_same_joins_a_pair_the_rule_calls_possible(self, app, profile):
        a, b = possible_pair(profile)
        assert dup.explain(a, b)["tier"] == dup.POSSIBLE
        assert dup.groups([a, b]) == []

        dup.set_link(a.id, b.id, verdict="same")
        (group,) = dup.groups_with_links([a, b], dup.load_links([a.id, b.id]))
        assert group.ids == (a.id, b.id)
        assert group.tier == dup.CONFIRMED
        (edge,) = group.edges
        assert edge.signals() == {
            dup.SOURCE_OWNER: {"status": dup.MET, "value": "same"}
        }

    def test_owner_different_splits_a_confirmed_pair(self, app, profile):
        a, b = confirmed_pair(profile)
        assert len(dup.groups([a, b])) == 1

        dup.set_link(a.id, b.id, verdict="different")
        refused = []
        found = dup.groups_with_links(
            [a, b], dup.load_links([a.id, b.id]), refused=refused
        )
        assert found == []
        assert [(e.a, e.b) for e in refused] == [(a.id, b.id)]

    def test_owner_different_survives_through_an_intermediate_row(self, app, profile):
        # a=b and b=c are both confirmed (shared ids), a and c are not the same.
        a = ide(profile, 93111111)
        b = yae(profile, 93111111)
        c = ide(profile, 93222222)
        # c is tied to b by the owner, then a-c is declared different.
        dup.set_link(b.id, c.id, verdict="same")
        dup.set_link(a.id, c.id, verdict="different")
        refused = []
        found = dup.groups_with_links(
            [a, b, c], dup.load_links([a.id, b.id, c.id]), refused=refused
        )
        members = [set(g.ids) for g in found]
        assert not any({a.id, c.id} <= m for m in members)
        # The owner's own same came first and stands; the rule's a-b edge lost.
        assert {b.id, c.id} in members
        assert [(e.a, e.b) for e in refused] == [(a.id, b.id)]

    def test_an_owner_same_blocked_by_an_owner_different_is_refused_and_reported(
        self, app, profile
    ):
        a, b = possible_pair(profile)
        c = foto(profile, 3, price=200000, area=999)
        dup.set_link(a.id, b.id, verdict="same")
        dup.set_link(b.id, c.id, verdict="same")
        dup.set_link(a.id, c.id, verdict="different")
        refused = []
        found = dup.groups_with_links(
            [a, b, c], dup.load_links([a.id, b.id, c.id]), refused=refused
        )
        assert [g.ids for g in found] == [(a.id, b.id)]
        assert [(e.a, e.b) for e in refused] == [tuple(sorted((b.id, c.id)))]
        assert refused[0].signals()[dup.SOURCE_OWNER]["value"] == "same"

    def test_a_pair_with_an_end_outside_the_rows_is_ignored(self, app, profile):
        a, b = possible_pair(profile)
        dup.set_link(a.id, b.id, verdict="same")
        assert dup.groups_with_links([a], dup.load_links([a.id, b.id])) == []

    def test_the_five_seed_aliases_still_confirm(self, app, profile):
        # (YAENCONTRE, 93478124) is a seed alias of (IDEALISTA, 93478124).
        a = ide(profile, 93478124)
        b = yae(profile, 106962112)
        assert len(dup.groups([a, b])) == 1
        assert len(dup.groups_with_links([a, b], dup.Links())) == 1


class TestTheForm:
    def _post(self, client, prop_id, **data):
        return client.post(f"/properties/{prop_id}/duplicate-link", data=data)

    def test_same_writes_one_row_and_redirects_to_the_anchor(self, client, profile):
        a, b = possible_pair(profile)
        response = self._post(client, a.id, other_id=b.id, decision="same")
        assert response.status_code == 302
        assert response.headers["Location"].endswith(
            f"/properties/{a.id}#duplicates-panel"
        )
        (row,) = link_rows()
        assert (row.verdict, row.source) == ("same", "owner")

    def test_clear_undoes_it(self, client, profile):
        a, b = possible_pair(profile)
        self._post(client, a.id, other_id=b.id, decision="different")
        self._post(client, a.id, other_id=b.id, decision="clear")
        assert PropertyDuplicateLink.query.count() == 0

    @pytest.mark.parametrize(
        "data",
        [
            {"other_id": "999999", "decision": "same"},
            {"other_id": "abc", "decision": "same"},
            {"other_id": "-3", "decision": "same"},
            {"other_id": "", "decision": "same"},
            {"decision": "same"},
            {"other_id": "SELF", "decision": "same"},
            {"other_id": "SELF", "decision": "merge"},
        ],
    )
    def test_bad_requests_write_nothing(self, client, profile, data):
        a, b = possible_pair(profile)
        data = {k: (str(a.id) if v == "SELF" else v) for k, v in data.items()}
        response = self._post(client, a.id, **data)
        assert response.status_code == 302
        assert PropertyDuplicateLink.query.count() == 0

    def test_an_unknown_property_is_a_404(self, client, app):
        assert self._post(client, 4242, other_id=1, decision="same").status_code == 404

    def test_a_listing_of_another_subscription_cannot_be_linked(self, client, profile):
        other_profile = SearchProfile(name="Asturias", is_active=True)
        db.session.add(other_profile)
        db.session.commit()
        a = yae(profile, 1, price=1)
        b = foto(other_profile, 2, price=1)
        self._post(client, a.id, other_id=b.id, decision="same")
        assert PropertyDuplicateLink.query.count() == 0

    def test_link_by_listing_id_and_by_url(self, client, profile):
        a = yae(profile, 1, price=1)
        b = ide(profile, 93555555)
        c = foto(profile, 777)
        self._post(client, a.id, other_ref="93555555", decision="same")
        self._post(
            client,
            a.id,
            other_ref="https://www.fotocasa.es/es/comprar/vivienda/x/777/d?utm_source=x",
            decision="different",
        )
        rows = {(r.property_low_id, r.property_high_id): r.verdict for r in link_rows()}
        assert rows == {
            tuple(sorted((a.id, b.id))): "same",
            tuple(sorted((a.id, c.id))): "different",
        }

    @pytest.mark.parametrize("ref", ["no such thing", "12345678", "https://x.io/nope/"])
    def test_a_reference_that_resolves_to_nothing_is_refused(
        self, client, profile, ref
    ):
        a, _ = possible_pair(profile)
        self._post(client, a.id, other_ref=ref, decision="same")
        assert PropertyDuplicateLink.query.count() == 0

    def test_a_reference_naming_two_listings_is_refused(self, client, profile):
        a = yae(profile, 1, price=1)
        ide(profile, 93555555)
        # Same idealista id on a second row: ambiguous, never guessed.
        _add(
            profile,
            "dupe",
            url="https://www.idealista.com/inmueble/93555555/",
            idealista_property_id=93555555,
        )
        self._post(client, a.id, other_ref="93555555", decision="same")
        assert PropertyDuplicateLink.query.count() == 0

    def test_a_reference_is_matched_inside_the_subscription_only(self, client, profile):
        other_profile = SearchProfile(name="Asturias", is_active=True)
        db.session.add(other_profile)
        db.session.commit()
        a = yae(profile, 1, price=1)
        ide(other_profile, 93555555)
        self._post(client, a.id, other_ref="93555555", decision="same")
        assert PropertyDuplicateLink.query.count() == 0

    def test_a_post_without_a_csrf_token_is_refused(self, app, profile):
        app.config["WTF_CSRF_ENABLED"] = True
        a, b = possible_pair(profile)
        response = app.test_client().post(
            f"/properties/{a.id}/duplicate-link",
            data={"other_id": b.id, "decision": "same"},
        )
        assert response.status_code == 400
        assert PropertyDuplicateLink.query.count() == 0


class TestThePanel:
    def _page(self, client, prop):
        response = client.get(f"/properties/{prop.id}")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "An error occurred while loading property details" not in body
        return body

    def test_the_panel_lists_each_publication_with_tier_and_reason(
        self, client, profile
    ):
        a, b = confirmed_pair(profile)
        c = yae(profile, 5, title=YAE_TITLE, price=250000, area=120)
        d = foto(profile, 6, title=FOTO_TITLE, price=250000, area=120)
        body = self._page(client, a)
        assert 'id="duplicates-panel"' in body
        assert f'data-duplicate-id="{b.id}"' in body
        assert f'data-duplicate-id="{c.id}"' not in body
        assert "Same home (confirmed)" in body
        assert "the same listing id on both portals" in body
        assert "yaencontre" in body
        assert "€90,000" in body
        assert f'name="other_id" value="{b.id}"' in body
        assert "Undo" not in body

        body = self._page(client, c)
        assert f'data-duplicate-id="{d.id}"' in body
        assert "Probably the same home" in body
        assert "the same place in the title" in body

    def test_a_possible_pair_reads_possibly(self, client, profile):
        a, b = possible_pair(profile)
        body = self._page(client, a)
        assert "Possibly the same home" in body
        assert 'data-duplicate-tier="possible"' in body

    def test_an_owner_link_shows_its_word_and_an_undo(self, client, profile):
        a, b = possible_pair(profile)
        dup.set_link(a.id, b.id, verdict="different")
        body = self._page(client, a)
        assert "Different home (your decision)" in body
        assert 'data-duplicate-tier="different"' in body
        assert "Undo" in body

    def test_a_link_the_rule_cannot_see_still_shows_on_both_pages(
        self, client, profile
    ):
        a = yae(profile, 1, price=1)
        b = foto(profile, 2, price=2)
        dup.set_link(a.id, b.id, verdict="same")
        for page, other in ((a, b), (b, a)):
            body = self._page(client, page)
            assert f'data-duplicate-id="{other.id}"' in body
            assert "Same home (your decision)" in body
            assert "Not matched by the rule" not in body

    def test_a_veto_is_disclosed(self, client, profile):
        a = ide(profile, 93111111, cadastral_reference="1234567VK1234A0001AB")
        b = yae(profile, 93111111, cadastral_reference="1234567VK1234A0002CD")
        dup.set_link(a.id, b.id, verdict="same")
        assert "cadastral references differ" in self._page(client, a)

    def test_the_panel_is_absent_when_there_is_nothing_but_the_link_box_stays(
        self, client, profile
    ):
        lone = yae(profile, 1, price=1, area=10)
        body = self._page(client, lone)
        assert 'id="duplicates-panel"' not in body
        assert 'id="duplicates-link"' in body
        assert body.count('name="other_ref"') == 1

    def test_the_panel_stays_inside_its_subscription(self, client, profile):
        other_profile = SearchProfile(name="Asturias", is_active=True)
        db.session.add(other_profile)
        db.session.commit()
        a = ide(profile, 93111111)
        yae(other_profile, 93111111)
        assert 'id="duplicates-panel"' not in self._page(client, a)

    def test_the_panel_in_spanish(self, client, profile):
        a, b = confirmed_pair(profile)
        with client.session_transaction() as session:
            session["language"] = "es"
        body = self._page(client, a)
        assert "Otras publicaciones de esta vivienda" in body
        assert "Misma vivienda (confirmado)" in body

    def test_the_panel_query_is_bounded(self, app, profile):
        """A page costs a handful of lookups however many rows the table holds."""
        from sqlalchemy import event

        a = yae(profile, 93111111, price=100000, area=100)
        for n in range(40):
            foto(profile, 1000 + n, price=100000, area=100)
        for n in range(60):
            foto(profile, 2000 + n, price=1 + n, area=1 + n)
        a_id = a.id
        db.session.expire_all()
        a = db.session.get(Property, a_id)  # loaded: no refresh query below
        statements = []

        def count(conn, cursor, statement, *args):
            statements.append(" ".join(statement.split()))

        event.listen(db.engine, "before_cursor_execute", count)
        try:
            found = dup.publications_for(a)
        finally:
            event.remove(db.engine, "before_cursor_execute", count)
        assert len(found) == 40  # the three-field block, nothing else
        # links_for + the three-field block + the id lookup; no cadastral
        # reference on the row, so no third lookup: never a query per row.
        assert len(statements) == 3, statements
        candidate_queries = [
            s for s in statements if "property_duplicate_link" not in s
        ]
        assert all("LIMIT" in s for s in candidate_queries), candidate_queries


class TestTheReport:
    def test_the_report_counts_applied_links_and_writes_nothing(
        self, app, profile, capsys
    ):
        a, b = possible_pair(profile)
        c, d = confirmed_pair(profile)
        dup.set_link(a.id, b.id, verdict="same")
        dup.set_link(c.id, d.id, verdict="different")
        before = (
            [(p.id, p.price, p.search_profile_id) for p in Property.query],
            [
                (link.id, link.verdict, link.decided_at, link.note)
                for link in PropertyDuplicateLink.query
            ],
        )
        assert report_duplicates.main(["--profile-id", str(profile.id)], app=app) == 0
        out = capsys.readouterr().out
        assert "owner decisions applied: 1 same, 1 different" in out
        # The owner-joined pair is a group; the split confirmed pair is not.
        assert "confirmed: 1 groups, 2 rows" in out
        assert "edges refused (would join a vetoed pair): 1" in out
        db.session.expire_all()
        after = (
            [(p.id, p.price, p.search_profile_id) for p in Property.query],
            [
                (link.id, link.verdict, link.decided_at, link.note)
                for link in PropertyDuplicateLink.query
            ],
        )
        assert before == after

    def test_a_same_refused_by_a_different_is_counted_apart(self, app, profile):
        a, b = possible_pair(profile)
        c = foto(profile, 3, price=1)
        dup.set_link(a.id, b.id, verdict="same")
        dup.set_link(b.id, c.id, verdict="same")
        dup.set_link(a.id, c.id, verdict="different")
        rows = report_duplicates.load(profile.id)[0]
        links = dup.load_links(r.id for r in rows)
        owner = report_duplicates.collect(rows, links=links)["owner_links"]
        assert owner == {"same_applied": 1, "same_refused": 1, "different_applied": 1}


def test_the_model_declares_the_constraints_migration_029_creates(app):
    table = PropertyDuplicateLink.__table__
    assert {c.name for c in table.constraints if c.name} == {
        "ck_property_duplicate_link_order",
        "ck_property_duplicate_link_verdict",
        "ck_property_duplicate_link_source",
        "ux_property_duplicate_link_pair",
    }
    assert {i.name for i in table.indexes} == {
        "ix_property_duplicate_link_property_high_id"
    }


@pytest.mark.parametrize(
    "values",
    [
        {"property_low_id": 2, "property_high_id": 1, "verdict": "same"},
        {"property_low_id": 1, "property_high_id": 1, "verdict": "same"},
        {"property_low_id": 1, "property_high_id": 2, "verdict": "maybe"},
        {"property_low_id": 1, "property_high_id": 2, "verdict": "same", "source": "x"},
    ],
)
def test_the_checks_refuse_what_the_writer_never_writes(app, profile, values):
    from sqlalchemy.exc import IntegrityError

    ide(profile, 1)
    ide(profile, 2)
    assert db.session.execute(select(Property.id)).all()
    db.session.add(PropertyDuplicateLink(**values))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
