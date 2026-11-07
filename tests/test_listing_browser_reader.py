"""Publication evidence must come from this attempt's owned, current listing."""

from email.message import EmailMessage
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest
from services import listing_browser as reader

URL = "https://www.idealista.com/inmueble/12345/"
NONCE = "a" * 32
WANTED = reader.navigation_url(URL, NONCE)
HTML = (
    """<html><head><link rel="canonical" href="%s"></head><body>
<h1>Casa en venta</h1><span class="info-data-price">285.000 <span>€</span></span>
<p>Referencia del anuncio: 12345</p></body></html>"""
    % URL
)


def write_archive(path, html=HTML, snapshot=WANTED, main=WANTED):
    message = EmailMessage()
    message["Snapshot-Content-Location"] = snapshot
    message.make_related()
    part = EmailMessage()
    part.set_content(html, subtype="html", charset="utf-8")
    part["Content-Location"] = main
    message.attach(part)
    path.write_bytes(message.as_bytes())
    return path


def classified(path, html=HTML, **changes):
    page = reader.read_archive(write_archive(path, html), WANTED)
    page.update(nonce=NONCE, url=WANTED, ready="complete", reader_elapsed_ms=10)
    page.update(changes)
    return reader.classify_page(URL, NONCE, page, 20)


def test_archive_confirms_primary_fields_without_exporting_page_or_inputs(tmp_path):
    html = HTML.replace(
        "</body>",
        """<form><h1>Private</h1><input value="secret">
    <textarea>this listing is no longer published</textarea></form>
    <aside><span class="info-data-price">1 €</span></aside></body>""",
    )
    page = reader.read_archive(write_archive(tmp_path / "private.mhtml", html), WANTED)
    assert page["h1"] == "Casa en venta" and page["price"] == "285.000 €"
    assert not page["removed_notice"]
    assert "secret" not in json.dumps(page) and str(tmp_path) not in json.dumps(page)
    assert classified(tmp_path / "ok.mhtml", html) == ("active", None)


@pytest.mark.parametrize("location", ["snapshot", "main"])
def test_archive_from_another_navigation_or_cached_nonce_is_rejected(
    tmp_path, location
):
    options = {location: URL}
    with pytest.raises(reader.ReaderFailure):
        reader.read_archive(write_archive(tmp_path / "old.mhtml", **options), WANTED)


def test_incomplete_archive_is_rejected(tmp_path):
    path = write_archive(tmp_path / "partial.mhtml")
    path.write_bytes(path.read_bytes().rsplit(b"--", 2)[0])
    with pytest.raises(reader.ReaderFailure):
        reader.read_archive(path, WANTED)


def test_archive_size_is_bounded_before_and_during_read(tmp_path, monkeypatch):
    path = write_archive(tmp_path / "large.mhtml")
    monkeypatch.setattr(reader, "MAX_ARCHIVE_BYTES", 100)
    with pytest.raises(reader.ReaderFailure):
        reader.read_archive(path, WANTED)
    actual_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p, *a, **kw: Mock(st_size=1) if p == path else actual_stat(p, *a, **kw),
    )
    with pytest.raises(reader.ReaderFailure):
        reader.read_archive(path, WANTED)


@pytest.mark.parametrize(
    "extra", ["<h1>Another house</h1>", '<span class="info-data-price">1 €</span>']
)
def test_ambiguous_primary_content_is_rejected(tmp_path, extra):
    with pytest.raises(reader.ReaderFailure):
        reader.read_archive(
            write_archive(
                tmp_path / "ambiguous.mhtml", HTML.replace("</body>", extra + "</body>")
            ),
            WANTED,
        )


def test_unmatched_closing_tag_does_not_escape_excluded_form(tmp_path):
    html = HTML.replace(
        "</body>",
        "<form></aside><h1>private</h1><textarea>secret</textarea></form></body>",
    )
    assert classified(tmp_path / "malformed.mhtml", html) == ("active", None)


@pytest.mark.parametrize(
    "notice,status",
    [
        ("This listing is no longer published", "removed"),
        ("Este inmueble se ha vendido", "sold"),
    ],
)
def test_explicit_removed_or_sold_without_price_requires_matching_reference(
    tmp_path, notice, status
):
    html = (
        '<link rel="canonical" href="%s"><p>%s. Referencia del anuncio: 12345</p>'
        % (URL, notice)
    )
    assert classified(tmp_path / "gone.mhtml", html) == (status, None)
    wrong = html.replace("anuncio: 12345", "anuncio: 99999")
    assert classified(tmp_path / "wrong.mhtml", wrong) == (
        "error",
        "availability_ambiguous",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical": "https://www.idealista.com/inmueble/99999/"},
        {"nonce": "b" * 32},
        {"main_url": URL},
        {"archive_url": URL},
        {"saved": False},
        {"reader_elapsed_ms": float("nan")},
        {"reader_elapsed_ms": True},
        {"reader_elapsed_ms": 30001},
        {"reader_elapsed_ms": 1100},
        {"h1": ""},
        {"price": "0 €"},
        {"price": "285000"},
        {"reserved_notice": True},
        {"blocked": True},
    ],
)
def test_incomplete_ambiguous_or_stale_evidence_never_confirms(tmp_path, changes):
    assert classified(tmp_path / "bad.mhtml", **changes)[0] == "error"


def test_owned_tab_archive_refuses_repurposed_tab_before_save(monkeypatch):
    tab = reader.OwnedTab(100)
    tab.window, tab.tab, tab.last_url = 12, 34, WANTED
    commands = []

    def command(body, **kwargs):
        commands.append(body)
        return "https://example.com/private"

    monkeypatch.setattr(tab, "command", command)
    with pytest.raises(reader.ReaderFailure):
        tab.archive(WANTED, Path("/unused"))
    assert commands == ["return URL of auditTab"]


def test_owned_tab_cleanup_targets_only_created_id_and_preserves_repurposed_tab(
    monkeypatch,
):
    scripts = []
    monkeypatch.setattr(
        reader, "apple", lambda script, deadline: scripts.append(script)
    )
    tab = reader.OwnedTab(100)
    tab.window, tab.tab, tab.last_url = 12, 34, WANTED
    tab.close()
    assert "window id 12" in scripts[0] and 'is "34"' in scripts[0]
    assert json.dumps(WANTED) in scripts[0] and json.dumps(URL) in scripts[0]
    assert '"about:blank"' in scripts[0]
    assert "contains (URL of auditTab) then close auditTab" in scripts[0]
    assert "close window" not in scripts[0] and "execute" not in scripts[0]


def test_remote_failure_cleans_owned_temp_and_restores_handlers(tmp_path, monkeypatch):
    import signal

    monkeypatch.setattr(reader.socket, "gethostname", lambda: reader.MACBOOK)
    monkeypatch.setattr(reader.subprocess, "run", lambda *a, **kw: Mock(returncode=0))
    monkeypatch.setattr(reader.Path, "home", lambda: tmp_path)
    closed = []

    class Tab:
        def __init__(self, deadline):
            pass

        def open(self):
            pass

        def navigate(self, url):
            raise reader.ReaderFailure("not_the_listing_page")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(reader, "OwnedTab", Tab)
    previous = signal.getsignal(signal.SIGALRM)
    with pytest.raises(reader.ReaderFailure):
        reader.read_native(URL, NONCE)
    assert closed == [True] and signal.getsignal(signal.SIGALRM) == previous
    folder = tmp_path / "Library/Caches/IdealistaRank/native-status"
    assert list(folder.iterdir()) == [folder / "reader.lock"]
    # A second attempt can acquire the same lock after failure.
    with pytest.raises(reader.ReaderFailure, match="not_the_listing_page"):
        reader.read_native(URL, NONCE)


def test_osascript_timeout_kills_and_reaps_only_its_process_group(monkeypatch):
    proc = Mock(pid=9876)
    proc.communicate.side_effect = [subprocess.TimeoutExpired("osascript", 1), ("", "")]
    launch = Mock(return_value=proc)
    killed = Mock()
    monkeypatch.setattr(reader.subprocess, "Popen", launch)
    monkeypatch.setattr(reader.os, "killpg", killed)
    with pytest.raises(subprocess.TimeoutExpired):
        reader.apple("return true", reader.time.monotonic() + 10)
    assert launch.call_args.kwargs["start_new_session"] is True
    assert killed.call_args.args[0] == 9876 and proc.communicate.call_count == 2


def test_browser_shell_sends_validated_source_over_stdin_with_constant_ssh_argv(
    tmp_path,
):
    capture = tmp_path / "capture.py"
    output = tmp_path / "captured.json"
    capture.write_text(
        'import sys,json\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({"argv":sys.argv[2:],"stdin":sys.stdin.read()}))\n'
    )
    browser = Path(__file__).resolve().parents[1] / "tools/browser.sh"
    script = 'function /usr/bin/ssh() { "$TEST_PYTHON" "$TEST_CAPTURE" "$TEST_OUTPUT" "$@"; }; source "$1" "${@:2}"'
    import os

    env = dict(
        os.environ,
        TEST_PYTHON=sys.executable,
        TEST_CAPTURE=str(capture),
        TEST_OUTPUT=str(output),
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            script,
            "--",
            str(browser),
            "--read-listing-status",
            URL,
            NONCE,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    got = json.loads(output.read_text())
    assert URL not in " ".join(got["argv"]) and NONCE not in " ".join(got["argv"])
    assert URL in got["stdin"] and NONCE in got["stdin"]
    assert "exec /usr/bin/python3 -" in got["argv"][-1]
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            script,
            "--",
            str(browser),
            "--read-listing-status",
            URL + "$(touch /tmp/never)",
            NONCE,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode != 0 and json.loads(output.read_text())["stdin"] == ""


@pytest.mark.parametrize(
    "html,reason",
    [
        (
            HTML.replace("</body>", "<h1>Another primary title</h1></body>"),
            "not_the_listing_page",
        ),
        (
            '<html><script src="https://geo.captcha-delivery.com/captcha/"></script></html>',
            "blocked",
        ),
    ],
)
def test_completed_refusal_propagates_instead_of_becoming_browser_timeout(
    tmp_path, monkeypatch, html, reason
):
    monkeypatch.setattr(reader.socket, "gethostname", lambda: reader.MACBOOK)
    monkeypatch.setattr(reader.subprocess, "run", lambda *a, **kw: Mock(returncode=0))
    monkeypatch.setattr(reader.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(reader, "REMOTE_SECONDS", 2)
    closed = []

    class Tab:
        def __init__(self, deadline):
            pass

        def open(self):
            pass

        def navigate(self, url):
            pass

        def location(self):
            return WANTED

        def command(self, command):
            return "false"

        def archive(self, url, path):
            write_archive(path, html)
            return True

        def close(self):
            closed.append(True)

    monkeypatch.setattr(reader, "OwnedTab", Tab)
    started = reader.time.monotonic()
    with pytest.raises(reader.ReaderFailure, match="^" + reason + "$"):
        reader.read_native(URL, NONCE)
    assert reader.time.monotonic() - started < 1.5
    assert closed == [True]


def test_navigate_timeout_keeps_the_requested_url_for_own_tab_cleanup(monkeypatch):
    tab = reader.OwnedTab(100)
    tab.window, tab.tab = 12, 34

    def command(body, **kwargs):
        raise subprocess.TimeoutExpired("osascript", 4)

    monkeypatch.setattr(tab, "command", command)
    with pytest.raises(subprocess.TimeoutExpired):
        tab.navigate(WANTED)
    assert tab.last_url == WANTED
    scripts = []
    monkeypatch.setattr(
        reader, "apple", lambda script, deadline: scripts.append(script)
    )
    monkeypatch.delattr(tab, "command")
    tab.close()
    assert json.dumps(WANTED) in scripts[0] and json.dumps(URL) in scripts[0]
    assert '"about:blank"' in scripts[0]


def test_browser_transport_socket_timeout_includes_existing_cleanup_margin(monkeypatch):
    from config import Config
    from services import subscription_transport as transport

    monkeypatch.setattr(Config, "AI_BRIDGE_URL", "http://127.0.0.1:5061")
    monkeypatch.setattr(Config, "AI_BRIDGE_TOKEN", "test-only-token")
    monkeypatch.setattr(Config, "AI_BRIDGE_SOCKET_MARGIN_SECONDS", 25)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"nonce": NONCE}).encode()
    urlopen = Mock(return_value=response)
    monkeypatch.setattr(transport.urllib.request, "urlopen", urlopen)
    transport.read_listing_status(URL, NONCE)
    assert urlopen.call_args.kwargs["timeout"] == 75
    assert json.loads(urlopen.call_args.args[0].data) == {"url": URL, "nonce": NONCE}


def test_unavailable_neighbor_in_seller_description_is_not_a_removed_listing(tmp_path):
    html = HTML.replace(
        "</body>", "<p>La parcela contigua ya no está disponible.</p></body>"
    )
    assert classified(tmp_path / "neighbor.mhtml", html) == ("active", None)


@pytest.mark.parametrize(
    "notice",
    [
        "Este inmueble ya no está disponible",
        "Este inmueble se ha vendido",
        "This listing is no longer published",
    ],
)
def test_live_title_or_price_conflicting_with_terminal_notice_is_ambiguous(
    tmp_path, notice
):
    html = HTML.replace("</body>", "<p>" + notice + "</p></body>")
    assert classified(tmp_path / "conflict.mhtml", html) == (
        "error",
        "availability_ambiguous",
    )
    # Even an unpriced live title is not a terminal-status banner.
    html = html.replace(
        '<span class="info-data-price">285.000 <span>€</span></span>', ""
    )
    assert classified(tmp_path / "unpriced.mhtml", html) == (
        "error",
        "availability_ambiguous",
    )


def test_explicit_removed_heading_without_live_fields_can_confirm_removal(tmp_path):
    html = (
        '<link rel="canonical" href="%s"><h1>This listing is no longer published</h1><p>Referencia del anuncio: 12345</p>'
        % URL
    )
    assert classified(tmp_path / "banner.mhtml", html) == ("removed", None)


@pytest.mark.parametrize(
    "prose",
    [
        "Just a moment from the beach and the town centre.",
        "Checking your browser will show the photo gallery.",
        "Please enable JS for the virtual tour.",
        "We verify you are human before responding to enquiries.",
        "The website uses captcha-delivery and ddos-guard.",
    ],
)
def test_seller_prose_is_not_a_technical_browser_challenge(tmp_path, prose):
    html = HTML.replace("</body>", "<p>" + prose + "</p></body>")
    assert classified(tmp_path / "prose.mhtml", html) == ("active", None)


def test_an_advertiser_link_is_not_a_challenge_frame(tmp_path):
    html = HTML.replace(
        "</body>",
        '<a href="https://geo.captcha-delivery.com/info/">Website details</a></body>',
    )
    assert classified(tmp_path / "link.mhtml", html) == ("active", None)


@pytest.mark.parametrize(
    "title", ["Just a moment...", "Checking your browser", "DDOS-GUARD"]
)
def test_exact_document_challenge_title_is_a_real_refusal(tmp_path, title):
    html = (
        "<html><head><title>"
        + title
        + "</title></head><body>Please wait.</body></html>"
    )
    with pytest.raises(reader.ReaderFailure, match="^blocked$"):
        reader.read_archive(
            write_archive(tmp_path / "challenge-title.mhtml", html), WANTED
        )


def test_challenge_phrase_inside_listing_document_title_is_not_a_challenge(tmp_path):
    html = HTML.replace(
        "<head>", "<head><title>Casa Just a moment from the beach — idealista</title>"
    )
    assert classified(tmp_path / "listing-title.mhtml", html) == ("active", None)


def test_nested_captcha_url_parameter_does_not_make_a_tour_a_challenge(tmp_path):
    html = HTML.replace(
        "</body>",
        '<iframe src="https://tour.example/view?url=https://geo.captcha-delivery.com/info/"></iframe></body>',
    )
    assert classified(tmp_path / "tour.mhtml", html) == ("active", None)


def test_protocol_relative_technical_challenge_is_detected(tmp_path):
    html = '<iframe src="//geo.captcha-delivery.com/captcha/"></iframe>'
    with pytest.raises(reader.ReaderFailure, match="^blocked$"):
        reader.read_archive(write_archive(tmp_path / "relative.mhtml", html), WANTED)


def test_creation_reply_timeout_recovers_only_its_marked_initial_tab(monkeypatch):
    scripts = []

    def apple(script, deadline):
        scripts.append(script)
        if len(scripts) == 1:
            raise subprocess.TimeoutExpired("osascript", 4)
        if len(scripts) == 2:
            return "12:34"
        return ""

    monkeypatch.setattr(reader, "apple", apple)
    tab = reader.OwnedTab(reader.time.monotonic() + 30)
    with pytest.raises(subprocess.TimeoutExpired):
        tab.open()
    assert tab.tab is None
    tab.close()
    assert len(scripts) == 3 and (tab.window, tab.tab) == (12, 34)
    assert "given name: " + json.dumps(tab.marker) in scripts[0]
    assert "every window whose given name is " + json.dumps(tab.marker) in scripts[1]
    assert (
        "every tab of auditWindow whose URL is " + json.dumps(tab.initial_url)
        in scripts[1]
    )
    assert "window id 12" in scripts[2] and 'is "34"' in scripts[2]
    assert "then close auditTab" in scripts[2] and "close window" not in scripts[2]


def test_missing_or_ambiguous_marker_never_closes_an_owner_window(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        reader, "apple", lambda script, deadline: scripts.append(script) or ""
    )
    tab = reader.OwnedTab(100)
    tab.creation_attempted = True
    tab.close()
    assert tab.tab is None and len(scripts) == 1
    assert '(count of ownedWindows) is not 1 then return ""' in scripts[0]
    assert "close" not in scripts[0]


def test_watchdog_signal_cannot_interrupt_archive_deletion(tmp_path, monkeypatch):
    import signal
    import threading

    monkeypatch.setattr(reader.socket, "gethostname", lambda: reader.MACBOOK)
    monkeypatch.setattr(reader.subprocess, "run", lambda *a, **kw: Mock(returncode=0))
    monkeypatch.setattr(reader.Path, "home", lambda: tmp_path)
    events = []

    class Tab:
        def __init__(self, deadline):
            pass

        def open(self):
            pass

        def navigate(self, url):
            pass

        def location(self):
            return WANTED

        def command(self, body):
            return "false"

        def archive(self, url, path):
            write_archive(path)
            return True

        def close(self):
            events.append("tab-closed")

    monkeypatch.setattr(reader, "OwnedTab", Tab)
    original_rmtree = reader.tempfile.TemporaryDirectory._rmtree

    def interrupted_rmtree(cls, path, *args, **kwargs):
        events.append(("timer-disarmed", signal.getitimer(signal.ITIMER_REAL)[0] == 0))
        fire.set()
        assert sent.wait(timeout=2)
        original_rmtree(path, *args, **kwargs)
        events.append("archive-deleted")

    monkeypatch.setattr(
        reader.tempfile.TemporaryDirectory, "_rmtree", classmethod(interrupted_rmtree)
    )
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, lambda *args: events.append("signal-delivered"))
    stop = threading.Event()
    fire = threading.Event()
    sent = threading.Event()

    def send_from_unblocked_worker():
        fire.wait()
        if stop.is_set():
            return
        signal.raise_signal(signal.SIGALRM)
        sent.set()
        stop.wait()

    worker = threading.Thread(target=send_from_unblocked_worker)
    worker.start()
    try:
        page = reader.read_native(URL, NONCE)
    finally:
        stop.set()
        fire.set()
        worker.join(timeout=2)
        signal.signal(signal.SIGALRM, previous)
    assert page["saved"] is True
    assert ("timer-disarmed", True) in events
    assert (
        events.index("tab-closed")
        < events.index("archive-deleted")
        < events.index("signal-delivered")
    )
    folder = tmp_path / "Library/Caches/IdealistaRank/native-status"
    assert list(folder.iterdir()) == [folder / "reader.lock"]


def test_unopened_reader_never_queries_chrome_during_cleanup(monkeypatch):
    apple = Mock()
    monkeypatch.setattr(reader, "apple", apple)
    reader.OwnedTab(100).close()
    apple.assert_not_called()
