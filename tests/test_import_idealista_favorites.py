"""Unit tests for Idealista.pt favorites → Property importer."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_TOOL = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "pt_ops"
    / "import_idealista_favorites.py"
)
_SPEC = importlib.util.spec_from_file_location("import_idealista_favorites", _TOOL)
assert _SPEC is not None and _SPEC.loader is not None
fav = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fav
_SPEC.loader.exec_module(fav)

FIXTURES = Path(__file__).parent / "fixtures" / "idealista_favorites"


def test_parse_favorites_html_reads_three_cards_and_skips_ads():
    html = (FIXTURES / "favorites_page.html").read_text(encoding="utf-8")
    listings = fav.parse_favorites_html(html)
    assert [row.listing_id for row in listings] == [90001001, 90001002, 90001003]

    first = listings[0]
    assert first.url == "https://www.idealista.pt/imovel/90001001/"
    assert first.price == 185000.0
    assert first.area == 60.0
    assert first.typology == "T2"
    assert first.bedrooms == 2
    assert first.is_rental is False
    assert "Porto" in (first.title or "")
    assert first.photos["items"] == [
        {
            "url": (
                "https://img4.idealista.pt/blur/WEB_LISTING-M/0/"
                "id.pro.pt.image.master/aa/bb/cc/90001001.jpg"
            )
        }
    ]
    assert first.photos["published"] >= 1

    house = listings[1]
    assert house.price == 620000.0
    assert house.area == 180.0
    assert house.typology == "T3"
    assert house.photos["items"] == [
        {
            "url": (
                "https://img3.idealista.pt/blur/WEB_LISTING-M/0/"
                "id.pro.pt.image.master/dd/ee/ff/90001002.jpg"
            )
        }
    ]

    rental = listings[2]
    assert rental.is_rental is True
    assert rental.price == 1450.0
    assert rental.area == 48.0
    assert rental.photos["items"] == []


def test_parse_favorites_json_reads_xhr_envelope():
    raw = (FIXTURES / "favorites_xhr.json").read_text(encoding="utf-8")
    listings = fav.parse_favorites_json(raw)
    assert [row.listing_id for row in listings] == [90002001, 90002002, 90002003]

    assert listings[0].price == 175000.0
    assert listings[0].area == 58.0
    assert listings[0].typology == "T2"
    assert listings[0].url == "https://www.idealista.pt/imovel/90002001/"
    assert listings[0].photos["items"] == [
        {
            "url": (
                "https://img4.idealista.pt/blur/WEB_LISTING-M/0/"
                "id.pro.pt.image.master/11/22/33/90002001.jpg"
            )
        }
    ]

    assert listings[1].price == 890000.0
    assert listings[1].area == 210.0
    assert listings[1].bedrooms == 4
    assert [item["url"] for item in listings[1].photos["items"]] == [
        "https://img3.idealista.pt/blur/WEB_DETAIL-L/0/id.pro.pt.image.master/44/55/66/90002002a.jpg",
        "https://img3.idealista.pt/blur/WEB_DETAIL-L/0/id.pro.pt.image.master/44/55/66/90002002b.jpg",
    ]

    assert listings[2].is_rental is True
    assert listings[2].price == 650.0
    assert listings[2].photos["items"] == []


def test_parse_favorites_json_reads_console_capture_helper():
    raw = (FIXTURES / "favorites_console.json").read_text(encoding="utf-8")
    listings = fav.parse_favorites_json(raw)
    assert [row.listing_id for row in listings] == [90003001, 90003002]
    assert listings[0].price == 165000.0
    assert listings[0].url == "https://www.idealista.pt/imovel/90003001/"
    assert listings[0].photos["items"] == [
        {
            "url": (
                "https://img4.idealista.pt/blur/WEB_LISTING-M/0/"
                "id.pro.pt.image.master/77/88/99/90003001.jpg"
            )
        }
    ]
    assert listings[1].price == 750000.0
    assert listings[1].photos["items"] == []


def test_fill_photos_if_missing_writes_once_and_skips_credentials():
    photo = (
        "https://img4.idealista.pt/blur/WEB_LISTING-M/0/"
        "id.pro.pt.image.master/aa/bb/cc/90001001.jpg"
    )
    listing = fav.FavoriteListing(
        listing_id=90001001,
        url="https://www.idealista.pt/imovel/90001001/",
        photos={
            "items": [
                {"url": photo},
                {"url": "https://img4.idealista.pt/x.jpg?apikey=nope"},
            ],
            "published": 2,
        },
    )
    prop = SimpleNamespace(enrichment={"import": {"source": "idealista"}})
    assert fav._fill_photos_if_missing(prop, listing) is True
    stored = prop.enrichment["import"]["photos"]["items"]
    assert stored == [{"url": photo}]
    assert fav._fill_photos_if_missing(prop, listing) is False


def test_load_dump_file_sniffs_by_suffix():
    html_rows = fav.load_dump_file(FIXTURES / "favorites_page.html")
    json_rows = fav.load_dump_file(FIXTURES / "favorites_xhr.json")
    assert len(html_rows) == 3
    assert len(json_rows) == 3


def test_blocked_datadome_html_raises_clear_error():
    html = (FIXTURES / "blocked_datadome.html").read_text(encoding="utf-8")
    with pytest.raises(fav.DumpBlockedError) as excinfo:
        fav.parse_favorites_html(html)
    message = str(excinfo.value).lower()
    assert "blocked" in message
    assert "dump" in message or "browser" in message


def test_plan_actions_create_mark_and_already():
    listings = [
        fav.FavoriteListing(
            listing_id=1,
            url="https://www.idealista.pt/imovel/1/",
            title="A",
            price=100.0,
        ),
        fav.FavoriteListing(
            listing_id=2,
            url="https://www.idealista.pt/imovel/2/",
            title="B",
            price=200.0,
        ),
        fav.FavoriteListing(
            listing_id=3,
            url="https://www.idealista.pt/imovel/3/",
            title="C",
            price=300.0,
        ),
    ]
    existing = {
        2: SimpleNamespace(id=20, is_favorite=False),
        3: SimpleNamespace(id=30, is_favorite=True),
    }
    plans = fav.plan_actions(listings, existing_by_listing_id=existing)
    assert [p["action"] for p in plans] == [
        "create",
        "mark_favorite",
        "already_favorite",
    ]
    assert plans[0]["source_email_id"] == "idealista:favorite:1"
    assert plans[1]["property_id"] == 20
    assert plans[2]["property_id"] == 30


def test_source_email_id_and_canonical_url():
    assert fav.source_email_id_for(90001001) == "idealista:favorite:90001001"
    assert fav.canonical_listing_url(90001001) == (
        "https://www.idealista.pt/imovel/90001001/"
    )


def test_cookie_loader_accepts_raw_header_without_echoing_secrets(tmp_path, capsys):
    path = tmp_path / "idealista-favorites.cookies"
    path.write_text("SESSION=secret-session-value; other=1\n", encoding="utf-8")
    path.chmod(0o600)
    header = fav.load_cookie_header(path)
    assert "secret-session-value" in header
    # The loader itself must not print the cookie value.
    captured = capsys.readouterr()
    assert "secret-session-value" not in captured.out
    assert "secret-session-value" not in captured.err


def test_cookie_loader_parses_netscape_jar(tmp_path):
    path = tmp_path / "jar.cookies"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        "www.idealista.pt\tTRUE\t/\tTRUE\t0\tSESSION\tabc123\n"
        "www.idealista.pt\tTRUE\t/\tFALSE\t0\tuid\tuser-1\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    header = fav.load_cookie_header(path)
    assert "SESSION=abc123" in header
    assert "uid=user-1" in header


def test_cookie_loader_refuses_authorization_header(tmp_path):
    path = tmp_path / "bad.cookies"
    path.write_text("Authorization: Bearer nope\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="Authorization"):
        fav.load_cookie_header(path)


def test_parse_only_cli_against_html_fixture(capsys):
    code = fav.main(["--dump", str(FIXTURES / "favorites_page.html"), "--parse-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "parsed=3" in out
    assert "90001001" in out
    assert "90001003" in out
    assert "photos_in_dump=2/3" in out


def test_help_mentions_logged_in_dump_and_photo_pitfall(capsys):
    with pytest.raises(SystemExit) as excinfo:
        fav.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "article.item" in out or "data-element-id" in out
    assert "photo_urls" in out
    assert "--fetch" in out
    assert "datadome" in out


def test_usage_hints_when_dump_has_no_photos():
    listings = [
        fav.FavoriteListing(
            listing_id=1,
            url="https://www.idealista.pt/imovel/1/",
            title="A",
        ),
        fav.FavoriteListing(
            listing_id=2,
            url="https://www.idealista.pt/imovel/2/",
            title="B",
        ),
    ]
    hints = fav.usage_hints(listings)
    joined = " ".join(hints).lower()
    assert "0/" in joined or "no photos" in joined or "0 listings" in joined
    assert "article" in joined
    assert fav.usage_hints([]) == []


def test_main_blocked_dump_exits_2(capsys):
    code = fav.main(["--dump", str(FIXTURES / "blocked_datadome.html"), "--parse-only"])
    assert code == 2
    err = capsys.readouterr().err
    assert "BLOCKED" in err


def test_looks_blocked_status_codes():
    assert fav._looks_blocked(403, "ok") == "http_403"
    assert fav._looks_blocked(200, "datadome challenge") == "body:datadome"
    assert fav._looks_blocked(200, "<article class='item'></article>") is None
