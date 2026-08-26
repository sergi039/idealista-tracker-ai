"""The link from a listing back to the dossier written about it.

Two things here are load bearing and neither is obvious from the feature.

**The href is the security boundary.** The URL comes out of a JSON column and
lands in `href="{{ ... }}"` on the property page. Jinja's autoescaping keeps
the value inside the attribute's quotes and says nothing whatever about its
scheme, so `javascript:alert(1)` stored in `enrichment["dossier"]["url"]`
would be script execution on that page. `normalise_url` is the whole guard,
and it is asserted here through the rendered page and not only as a unit --
a guard that is correct in the service and not wired into the template is the
defect this repository keeps rediscovering (#309).

**A malformed block reads as no dossier, never as an error.** `read_dossier`
is called once per row by anything that serialises a list, and
`routes/main_routes.py` turns a template error into a redirect, so a single
row carrying a hand-edited block could take a whole page down and present as
"the listing vanished". Six shapes are checked against that.
"""

import pytest

from app import create_app, db
from models import Property, SearchProfile
from services.dossier import (
    DossierError,
    clear_dossier,
    has_dossier,
    normalise_url,
    read_dossier,
    record_dossier,
)


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def prop(app):
    profile = SearchProfile(name="Dossier test", is_active=True)
    db.session.add(profile)
    db.session.flush()
    row = Property(
        source_email_id="manual:dossier-test",
        search_profile_id=profile.id,
        title="Casa en Seiruga",
        url="https://www.idealista.com/inmueble/105654216/",
        price=294000,
        area=302,
        municipality="Malpica de Bergantiños",
    )
    db.session.add(row)
    db.session.commit()
    return row


# --- the reader is total and fail-closed -----------------------------------


def test_no_block_reads_as_no_dossier(prop):
    assert read_dossier(prop) is None
    assert has_dossier(prop) is False


@pytest.mark.parametrize(
    "block",
    [
        None,
        "https://1282.cervantes50.com",  # a string where a mapping belongs
        [],
        {},  # a mapping with no url
        {"url": None},
        {"url": 17},
        {"url": ""},
        {"url": "   "},
        {"url": "1282.cervantes50.com"},  # no scheme -> no host either
        {"url": "/properties/1282"},  # relative
        {"url": "https://"},  # scheme without a host
    ],
)
def test_malformed_blocks_read_as_no_dossier(prop, block):
    """Every shape a hand-edited column can hold answers `None`, not raises.

    The list calls this once per row; one bad row must not take the page down.
    """
    prop.enrichment = {"dossier": block}
    assert read_dossier(prop) is None


@pytest.mark.parametrize(
    "scheme",
    ["javascript", "data", "vbscript", "file", "mailto", "JavaScript", "JAVASCRIPT"],
)
def test_dangerous_schemes_are_refused(prop, scheme):
    """`javascript:` in an href is script execution on the property page."""
    prop.enrichment = {"dossier": {"url": f"{scheme}:alert(1)"}}
    assert read_dossier(prop) is None
    assert normalise_url(f"{scheme}:alert(1)") is None


def test_url_longer_than_the_cap_is_refused(prop):
    long_url = "https://example.com/" + ("a" * 3000)
    assert normalise_url(long_url) is None


def test_http_and_https_are_accepted():
    assert normalise_url("https://1282.cervantes50.com") == "https://1282.cervantes50.com"
    assert normalise_url("http://127.0.0.1:5001/properties/1282") is not None


def test_title_falls_back_to_the_host(prop):
    prop.enrichment = {"dossier": {"url": "https://1282.cervantes50.com/index.html"}}
    assert read_dossier(prop)["title"] == "1282.cervantes50.com"


def test_stored_title_wins_and_is_trimmed(prop):
    prop.enrichment = {
        "dossier": {"url": "https://1282.cervantes50.com", "title": "  Seiruga  "}
    }
    assert read_dossier(prop)["title"] == "Seiruga"


# --- the writer -------------------------------------------------------------


def test_record_and_read_round_trip(prop):
    record_dossier(
        prop, url="https://1282.cervantes50.com", title="Seiruga", by="owner"
    )
    stored = read_dossier(prop)
    assert stored["url"] == "https://1282.cervantes50.com"
    assert stored["title"] == "Seiruga"
    assert stored["by"] == "owner"
    assert stored["recorded_at"]


def test_record_keeps_the_other_enrichment_blocks(prop):
    """`enrichment` is one column: writing a block must not drop its siblings."""
    prop.enrichment = {"sea": {"status": "ok", "distance_m": 394.7}}
    db.session.commit()
    record_dossier(prop, url="https://1282.cervantes50.com")
    assert prop.enrichment["sea"]["distance_m"] == 394.7
    assert read_dossier(prop) is not None


def test_writer_refuses_what_the_reader_would_refuse(prop):
    """A stored value the page will not render is a link that exists nowhere."""
    with pytest.raises(DossierError):
        record_dossier(prop, url="javascript:alert(1)")
    with pytest.raises(DossierError):
        record_dossier(prop, url="1282.cervantes50.com")
    assert read_dossier(prop) is None


def test_clear_removes_the_pointer_and_nothing_else(prop):
    prop.enrichment = {"sea": {"status": "ok"}}
    db.session.commit()
    record_dossier(prop, url="https://1282.cervantes50.com")
    assert clear_dossier(prop) is True
    assert read_dossier(prop) is None
    assert prop.enrichment["sea"]["status"] == "ok"
    # Clearing a row that has none says so rather than raising.
    assert clear_dossier(prop) is False


# --- wired into the page, not only into the service -------------------------


def test_property_page_renders_the_link(app, prop):
    record_dossier(
        prop, url="https://1282.cervantes50.com", title="Seiruga · Malpica"
    )
    client = app.test_client()
    response = client.get(f"/properties/{prop.id}")
    assert response.status_code == 200, "a 302 here is the template failing"
    body = response.get_data(as_text=True)
    assert "https://1282.cervantes50.com" in body
    assert "Seiruga" in body


def test_property_page_shows_no_link_without_one(app, prop):
    client = app.test_client()
    response = client.get(f"/properties/{prop.id}")
    assert response.status_code == 200
    assert "cervantes50" not in response.get_data(as_text=True)


def test_property_page_does_not_render_a_javascript_url(app, prop):
    """The guard is asserted where it matters: in the rendered href.

    **What this deliberately does not assert** is that the string is absent
    from the page. It is present, and legitimately so: `to_dict()` carries the
    raw `enrichment` column and the page ships it as
    `window.propertyData = {{ property.to_dict() | tojson }}`, where `tojson`
    escapes it into a JSON string literal inside the existing `<script>`. That
    is data, not an href, and nothing executes it. Asserting "not in body"
    passed for the wrong reason and failed for the wrong reason -- the first
    version of this test went red against that serialisation while the href
    was already correct.

    Mutation note: removing the scheme check in `normalise_url` reddens this
    test and the unit ones together. Removing only the template's use of
    `dossier_link` reddens the render test above but not this one -- which is
    why both exist.
    """
    import re

    prop.enrichment = {"dossier": {"url": "javascript:alert(1)"}}
    db.session.commit()
    client = app.test_client()
    response = client.get(f"/properties/{prop.id}")
    assert response.status_code == 200, "a malformed block must not break the page"
    body = response.get_data(as_text=True)
    hrefs = re.findall(r"""href\s*=\s*["']([^"']*)["']""", body)
    assert not any(h.strip().lower().startswith("javascript:") for h in hrefs), (
        "a stored javascript: URL reached an href"
    )


def test_to_dict_carries_the_dossier(prop):
    assert prop.to_dict()["dossier"] is None
    record_dossier(prop, url="https://1282.cervantes50.com")
    assert prop.to_dict()["dossier"]["url"] == "https://1282.cervantes50.com"
