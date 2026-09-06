"""The photograph a yaencontre alert email carries, for rows already here.

yaencontre answers DataDome to every request from both machines, so the alert
email has always been the only source for those rows -- and #548 captures the
card's photograph only for listings ingested since it shipped. Measured on
production 2026-09-06: 737 yaencontre rows, **26 with any description** and 82
with a photograph, so 655 can be judged from neither a picture nor a sentence.
The emails that carried those pictures are still in the mailbox.

What this file pins, and each of the four is a way the tool could do harm
rather than good:

* it fills a row that has none, from the REAL committed alert fixture;
* it never creates a row -- a card naming a listing this database does not hold
  is counted and skipped, because creating one is ingestion and belongs to the
  machine that owns the mailbox, with the profile resolution and dedup key
  `fotocasa_import.build_property` applies;
* it never overwrites a photograph a row already carries;
* and it never touches the UID cursor, which is the ingester's: moving it would
  make the ingester skip mail nobody has read, the one way a read-only tool
  could lose listings.

The IMAP side is mocked throughout -- `tests/network_guard.py` refuses every
connection that leaves this machine, and would fail the run rather than let one
through silently.
"""

import email as email_lib
from unittest.mock import MagicMock, patch

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services import portal_photos, yaencontre_source
from tests import setup_test_environment
from utils import backfill_yaencontre_photos as tool

FIXTURE = "tests/data/yaencontre_alert_boiro.html"


def _raw_email(html: str) -> bytes:
    """One real alert body wrapped as the message IMAP would hand back."""
    message = email_lib.message.EmailMessage()
    message["From"] = "no-reply@envios.yaencontre.com"
    message["Subject"] = "Nuevas viviendas"
    message.set_content("plain text fallback")
    message.add_alternative(html, subtype="html")
    return message.as_bytes()


def _fixture():
    with open(FIXTURE, encoding="utf-8", errors="replace") as handle:
        return handle.read()


@pytest.fixture
def app():
    setup_test_environment()
    application = create_app()
    application.config["TESTING"] = True
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def profile(app):
    row = SearchProfile(name="Galicia · costa", is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def cards():
    """The real email, read by the real parser."""
    return yaencontre_source.cards_in_email(_fixture())


def _row(profile, card, **kwargs):
    row = Property(
        source_email_id=f"{yaencontre_source.SOURCE_NAME}:{card.listing_id}",
        title=card.title or "Casa",
        url=card.url,
        municipality=card.municipality,
        search_profile_id=profile.id,
        **kwargs,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _run(bodies, apply=False, limit=0):
    """The work, under the test's own app context and database."""
    return tool.fill_from_bodies(bodies, apply=apply, limit=limit)


class TestItFillsWhatOnlyTheEmailKnows:
    def test_a_row_with_no_photograph_gets_the_card_s_own(self, app, profile, cards):
        card = cards[0]
        row = _row(profile, card, enrichment={"import": {"source": "yaencontre"}})
        assert portal_photos.read_photos(row)["state"] == "not_captured"

        _run([_fixture()], apply=True)
        db.session.refresh(row)

        reading = portal_photos.read_photos(row)
        assert reading["state"] == "captured"
        assert reading["count"] == 1
        assert reading["photos"][0]["url"] == card.photos["items"][0]["url"]
        assert "media.yaencontre.com" in reading["photos"][0]["url"]

    def test_a_dry_run_writes_nothing(self, app, profile, cards):
        row = _row(profile, cards[0], enrichment={"import": {"source": "yaencontre"}})

        _run([_fixture()])
        db.session.refresh(row)

        assert portal_photos.read_photos(row)["state"] == "not_captured"


class TestTheThreeWaysItCouldDoHarm:
    def test_it_never_creates_a_row(self, app, profile, cards):
        """Ten cards, no rows: creating them is ingestion, which belongs to the
        machine that owns the mailbox and applies the profile resolution, the
        dedup key and the advertiser rules this tool has none of."""
        before = Property.query.count()

        _run([_fixture()], apply=True)

        assert Property.query.count() == before == 0

    def test_it_never_overwrites_a_photograph_the_row_already_has(
        self, app, profile, cards
    ):
        """Whatever is there came from the ingester, from this same email, when
        the listing arrived — at least as good as what this pass would find."""
        kept = "https://media.yaencontre.com/img/photo/w630/1/already-here.jpg"
        row = _row(
            profile,
            cards[0],
            enrichment={
                "import": {
                    "source": "yaencontre",
                    "photos": {"items": [{"url": kept}], "published": 1},
                }
            },
        )

        _run([_fixture()], apply=True)
        db.session.refresh(row)

        assert portal_photos.read_photos(row)["photos"][0]["url"] == kept

    def test_the_write_takes_the_row_under_a_lock(self, app, profile, cards):
        """Asserted as a CALL, with its arguments, because SQLite has no row
        lock to observe -- the shape `owner_review`'s own test records. So this
        proves the writer asks for `FOR UPDATE` and commits its own
        transaction; it does not prove PostgreSQL then serialises anything. A
        mutation flipping `locked=True` to `False` left every other test in
        this file green."""
        _row(profile, cards[0], enrichment={"import": {"source": "yaencontre"}})

        with patch.object(tool, "locked_write", wraps=tool.locked_write) as locker:
            _run([_fixture()], apply=True)

        assert locker.call_count == 1
        assert locker.call_args.kwargs == {"locked": True, "commit": True}

    def test_a_row_that_already_answers_is_never_even_locked(self, app, profile, cards):
        """The outer check exists to keep the run off rows it has nothing to do
        for -- 82 of the 737 on production carry a photograph already. Taking
        `FOR UPDATE` on each of them costs contention for nothing, and the
        inner re-read under the lock (which is what actually prevents the
        overwrite) cannot express that."""
        _row(
            profile,
            cards[0],
            enrichment={
                "import": {
                    "source": "yaencontre",
                    "photos": {
                        "items": [{"url": "https://x.test/a.jpg"}],
                        "published": 1,
                    },
                }
            },
        )

        with patch.object(tool, "locked_write") as locker:
            counts = _run([_fixture()], apply=True)

        locker.assert_not_called()
        assert counts["already"] == 1

    def test_the_mailbox_is_opened_read_only_and_the_cursor_is_untouched(self, app):
        """Asserted against `_bodies`, the only function that opens a mailbox.

        The first version of this test drove the work function instead, which
        never touches IMAP at all -- so it could not have failed. The cursor is
        the ingester's: moving it would make the ingester skip mail nobody has
        read, the one way a read-only tool could lose listings.
        """
        client = MagicMock()
        client.__enter__.return_value = client
        client.search.return_value = [11, 12]
        client.fetch.return_value = {
            11: {b"RFC822": _raw_email(_fixture())},
            12: {b"RFC822": _raw_email(_fixture())},
        }

        with (
            patch.object(tool, "IMAPClient", return_value=client),
            patch.object(
                tool.Config,
                "YAENCONTRE_ALERT_SENDERS",
                "no-reply@envios.yaencontre.com",
            ),
            patch.object(tool.Config, "IMAP_HOST", "imap.gmail.com"),
            patch.object(tool.Config, "IMAP_USER", "x@example.com"),
            patch.object(tool.Config, "IMAP_PASSWORD", "secret"),
            patch(
                "services.property_imap_service.PropertyIMAPService._save_last_seen_uid"
            ) as saver,
        ):
            bodies = tool._bodies(0)

        assert len(bodies) == 2
        saver.assert_not_called()
        assert client.select_folder.call_args_list, "no folder was selected"
        for call in client.select_folder.call_args_list:
            assert call.kwargs.get("readonly") is True, call
        # It asks for yaencontre mail only -- never the whole mailbox, and
        # never the idealista term the ingester's own query carries.
        query = " ".join(
            str(a) for call in client.search.call_args_list for a in call.args
        )
        assert "yaencontre" in query
        assert "idealista" not in query

    def test_an_unconfigured_machine_refuses_rather_than_reading_everything(self, app):
        """Empty senders means this machine is configured not to read that
        portal's mail. Falling through to `ALL` would read the whole mailbox."""
        with patch.object(tool.Config, "YAENCONTRE_ALERT_SENDERS", ""):
            with pytest.raises(SystemExit):
                tool._bodies(0)

    def test_a_row_whose_portal_published_none_is_left_alone(self, app, profile, cards):
        """`{"items": [], "published": 0}` is a measurement — the ingester read
        the card and it carried nothing. Filling it later would overwrite a
        fact with a different one."""
        row = _row(
            profile,
            cards[0],
            enrichment={
                "import": {
                    "source": "yaencontre",
                    "photos": {"items": [], "published": 0},
                }
            },
        )

        _run([_fixture()], apply=True)
        db.session.refresh(row)

        assert portal_photos.read_photos(row)["state"] == "none_published"


class TestTheScope:
    def test_every_card_in_the_real_email_carries_one(self, cards):
        """If this ever stops being true the tool is pointless, and the reason
        would be a change in the email rather than in this repository."""
        assert len(cards) == 10
        assert all(card.photos.get("items") for card in cards)

    def test_the_limit_stops_the_run(self, app, profile, cards):
        for card in cards[:3]:
            _row(profile, card, enrichment={"import": {"source": "yaencontre"}})

        _run([_fixture()], apply=True, limit=1)

        filled = [
            row
            for row in Property.query.all()
            if portal_photos.read_photos(row)["state"] == "captured"
        ]
        assert len(filled) == 1
