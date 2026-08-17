"""A machine that does not ingest on a tick does not ingest on one click either.

#376 made `AUTO_START_SCHEDULER` fail-closed in every place that decides a
default, which stopped a dev checkout from ingesting on a cron tick. It left the
other path open: `POST /api/ingest/email/run` -- the Manual Sync button in the
navbar of every page, CSRF-exempt and behind no authentication -- read the same
Gmail label on one click whatever that flag said. These tests pin the endpoint's
refusal, the button's absence, and the one property that makes the refusal worth
anything: it happens *before* the mailbox is touched.

What is deliberately NOT claimed here: an ad-hoc script run through
`docker exec ... python -` builds the service directly and never reaches Flask.
No HTTP-layer test can cover that, and pretending otherwise would be the kind of
guard-presented-as-complete this repository keeps paying for.
"""

import json
from unittest.mock import patch

import pytest

from app import create_app, db
from tests import setup_test_environment


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


# The role is set through `app.config`, not the `Config` class: that is where
# the scheduler reads it (services/scheduler_service.py, app.py's
# should_start_scheduler) and where four pre-existing tests set it. A fixture
# patching the class instead would test a source nothing else consults.
@pytest.fixture
def not_an_ingester(app):
    app.config["AUTO_START_SCHEDULER"] = False


@pytest.fixture
def an_ingester(app):
    app.config["AUTO_START_SCHEDULER"] = True


class TestEndpointRefuses:
    def test_refuses_with_409_and_a_reason(self, client, not_an_ingester):
        response = client.post("/api/ingest/email/run")

        assert response.status_code == 409
        body = json.loads(response.data)
        assert body["success"] is False
        assert body["reason"] == "not_an_ingester"
        # The message has to name what to do, not just say no: the deployment is
        # the machine that sets the flag.
        assert "AUTO_START_SCHEDULER" in body["error"]

    @patch("services.property_imap_service.PropertyIMAPService")
    def test_never_touches_the_mailbox(self, mock_imap, client, not_an_ingester):
        response = client.post("/api/ingest/email/run")

        assert response.status_code == 409
        # The whole point. A guard that refuses after opening the mailbox has
        # already read someone else's mail and possibly moved a cursor.
        mock_imap.assert_not_called()

    def test_guard_runs_before_the_body_is_parsed(self, client, not_an_ingester):
        # Malformed JSON would raise inside the request parsing that used to be
        # the function's first act. A 409 here proves the guard precedes it, so
        # ordering cannot regress unnoticed.
        response = client.post(
            "/api/ingest/email/run",
            data="{not json",
            content_type="application/json",
        )

        assert response.status_code == 409
        assert json.loads(response.data)["reason"] == "not_an_ingester"

    @patch("services.property_imap_service.PropertyIMAPService")
    def test_allows_the_ingester(self, mock_imap, client, an_ingester):
        mock_imap.return_value.run_ingestion.return_value = 3

        response = client.post("/api/ingest/email/run")

        assert response.status_code == 200
        assert json.loads(response.data)["processed_count"] == 3


class TestButtonIsAbsentWhereTheEndpointRefuses:
    # The control exists in THREE templates, not one: the navbar (base.html), the
    # empty-state of the list (properties.html) and Full Sync in settings
    # (property_settings.html). The first version of this test checked only
    # /properties and failed, which is how the other two were found -- so every
    # surface that can render it gets its own case.
    @pytest.mark.parametrize("path", ["/properties", "/settings/properties"])
    def test_hidden_on_a_machine_that_does_not_ingest(
        self, client, not_an_ingester, path
    ):
        html = client.get(path).get_data(as_text=True)

        assert "ingest/email/run" not in html, f"{path} still offers the control"

    @pytest.mark.parametrize("path", ["/properties", "/settings/properties"])
    def test_shown_on_the_ingester(self, client, an_ingester, path):
        html = client.get(path).get_data(as_text=True)

        assert "ingest/email/run" in html, f"{path} lost the control"

    def test_empty_state_does_not_promise_a_sync_it_cannot_offer(
        self, client, not_an_ingester
    ):
        # The empty list used to read "run a manual sync to fetch new listings"
        # right above the button. Hiding the button alone would leave the page
        # telling the reader to press something that is not there.
        html = client.get("/properties").get_data(as_text=True)

        assert "run a manual sync" not in html


class TestPolicyModule:
    def test_verdict_carries_a_reason_either_way(self, app):
        from services.ingest_policy import ingest_verdict, machine_is_ingester

        app.config["AUTO_START_SCHEDULER"] = True
        assert ingest_verdict() == (True, "ingester")
        assert machine_is_ingester() is True

        app.config["AUTO_START_SCHEDULER"] = False
        assert ingest_verdict() == (False, "not_an_ingester")
        assert machine_is_ingester() is False

    def test_reads_the_source_the_scheduler_reads(self, app, monkeypatch):
        # The two readings of this flag are built from the environment at
        # different moments -- app.config in create_app(), Config at import --
        # so they can disagree. When they do, the guard must follow the one the
        # scheduler obeys, or it refuses a manual run on a machine that is
        # already ingesting on a cron tick.
        from config import Config
        from services.ingest_policy import ingest_verdict

        monkeypatch.setattr(Config, "AUTO_START_SCHEDULER", False)
        app.config["AUTO_START_SCHEDULER"] = True
        assert ingest_verdict().allowed is True, "app.config must win"

        monkeypatch.setattr(Config, "AUTO_START_SCHEDULER", True)
        app.config["AUTO_START_SCHEDULER"] = False
        assert ingest_verdict().allowed is False, "app.config must win"

    def test_falls_back_to_config_outside_an_app_context(self, monkeypatch):
        # A `docker compose run` sibling or a CLI import has no app.config to
        # ask, and must not crash on current_app.
        from config import Config
        from services.ingest_policy import ingest_verdict

        monkeypatch.setattr(Config, "AUTO_START_SCHEDULER", True)
        assert ingest_verdict() == (True, "ingester")
        monkeypatch.setattr(Config, "AUTO_START_SCHEDULER", False)
        assert ingest_verdict() == (False, "not_an_ingester")

    def test_no_second_flag_was_invented(self):
        # The rule reuses the one fact the configuration already carries. A
        # separate INGEST_ENABLED would be a second thing to set and forget, and
        # a machine could then declare the two halves of one fact differently.
        from pathlib import Path

        source = (
            Path(__file__).parent.parent / "services" / "ingest_policy.py"
        ).read_text(encoding="utf-8")

        assert "INGEST_ENABLED" not in source.replace("Adding `INGEST_ENABLED`", ""), (
            "ingest_policy must not read a flag of its own"
        )
