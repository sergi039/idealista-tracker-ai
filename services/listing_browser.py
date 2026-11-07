"""Read public Idealista fields in an owned normal-Chrome tab on the MacBook.

Stdlib-only source is sent over SSH stdin. Chrome's supported MHTML save does
not need JavaScript through Apple Events. Archives never leave the MacBook.
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import fcntl
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

MACBOOK = "MacBook-Pro007.local"
REMOTE_SECONDS = 30
CLEANUP_SECONDS = 4
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
OSASCRIPT = "/usr/bin/osascript"


def identity(url):
    if not isinstance(url, str):
        raise ValueError("unsupported public listing URL")
    match = re.fullmatch(
        r"https://(?:www\.)?idealista\.com/(?:en/)?inmueble/([0-9]{1,20})/?",
        url,
        flags=re.ASCII,
    )
    if not match:
        raise ValueError("unsupported public listing URL")
    return "idealista", "idealista.com", match.group(1)


def validate_request(url, nonce):
    identity(url)
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise ValueError("invalid observation nonce")


def navigation_url(url, nonce):
    validate_request(url, nonce)
    # Canonicalize only this fixed known transformation before navigation.
    language = "en/" if "/en/inmueble/" in url else ""
    return (
        "https://www.idealista.com/"
        + language
        + "inmueble/"
        + identity(url)[2]
        + "/?availability_check="
        + nonce
    )


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def classify_page(url, nonce, page, elapsed_ms):
    """Validate current publication evidence, never invent a measured HTTP code."""
    bad = ("error", "not_the_listing_page")
    try:
        expected = navigation_url(url, nonce)
        identifier = identity(url)[2]
        if not isinstance(page, dict) or page.get("nonce") != nonce:
            return bad
        refusal = page.get("refusal")
        if refusal in {
            "browser_busy",
            "missing_configuration",
            "browser_unavailable",
            "blocked",
            "timeout",
            "not_the_listing_page",
        }:
            return "error", refusal
        duration = page.get("reader_elapsed_ms")
        if (
            not _number(elapsed_ms)
            or not 0 <= elapsed_ms <= 75000
            or not _number(duration)
            or not 0 <= duration <= REMOTE_SECONDS * 1000
            or duration > elapsed_ms + 1000
        ):
            return bad
        if (
            page.get("url") != expected
            or page.get("archive_url") != expected
            or page.get("main_url") != expected
            or page.get("saved") is not True
            or page.get("ready") != "complete"
        ):
            return bad
        if page.get("blocked"):
            return "error", "blocked"
        if identity(page.get("canonical")) != identity(url):
            return bad
        # A removed listing can have neither a title nor price.
        if page.get("removed_notice") or page.get("sold_notice"):
            if page.get("reference") == identifier:
                return ("removed" if page.get("removed_notice") else "sold"), None
            return "error", "availability_ambiguous"
        if page.get("reserved_notice"):
            return "error", "availability_ambiguous"
        h1, price = page.get("h1"), page.get("price")
        if not isinstance(h1, str) or not h1.strip() or len(h1) > 1000:
            return bad
        if (
            not isinstance(price, str)
            or len(price) > 100
            or not re.search(r"[1-9][0-9.,\s]*\s*€", price)
        ):
            return bad
        return "active", None
    except (ValueError, TypeError, OverflowError):
        return bad


class PublicFields(HTMLParser):
    """Keep only primary public fields; scripts, forms and related areas excluded."""

    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    EXCLUDED = {
        "script",
        "style",
        "form",
        "input",
        "textarea",
        "aside",
        "nav",
        "footer",
        "noscript",
        "template",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.canonicals, self.headings, self.prices, self.public = [], [], [], []
        self.public_size = 0
        self.overflow = False
        self.challenge = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if any(
            re.search(
                r"https?://(?:[a-z0-9-]+\.)?captcha-delivery\.com/",
                attrs.get(key, ""),
                re.I,
            )
            for key in ("src", "href")
        ):
            self.challenge = True
        skip = (
            bool(self.stack and self.stack[-1][1])
            or tag in self.EXCLUDED
            or "contenteditable" in attrs
        )
        heading = price = None
        if not skip:
            if tag == "link" and "canonical" in attrs.get("rel", "").split():
                self.canonicals.append(attrs.get("href"))
            if tag == "h1":
                heading = []
                self.headings.append(heading)
            if "info-data-price" in attrs.get("class", "").split():
                price = []
                self.prices.append(price)
        if tag not in self.VOID:
            self.stack.append((tag, skip, heading, price))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if self.stack and self.stack[-1][1]:
            return
        self.public_size += len(value)
        if self.public_size > 100000:
            self.overflow = True
            return
        self.public.append(value)
        for _, _, heading, price in self.stack:
            if heading is not None:
                heading.append(value)
            if price is not None:
                price.append(value)

    def result(self):
        text = " ".join(self.public)
        low = text.lower()
        if self.challenge or re.search(
            r"checking your browser|please enable js|just a moment|verify you are human|captcha-delivery|ddos-guard",
            low,
        ):
            raise ReaderFailure("blocked")
        if (
            self.overflow
            or len(self.canonicals) != 1
            or len(self.headings) > 1
            or len(self.prices) > 1
        ):
            raise ReaderFailure("not_the_listing_page")
        reference = re.search(
            r"(?:Listing reference|Referencia del anuncio)\s*:?\s*([0-9]+)", text, re.I
        )
        return {
            "canonical": self.canonicals[0],
            "h1": "".join(self.headings[0]).strip() if self.headings else "",
            "price": "".join(self.prices[0]).strip() if self.prices else "",
            "reference": reference.group(1) if reference else None,
            "blocked": bool(
                re.search(
                    r"checking your browser|please enable js|just a moment|verify you are human|captcha-delivery|ddos-guard",
                    low,
                )
            ),
            "removed_notice": bool(
                re.search(
                    r"this listing is no longer published|listing is no longer available|ya no está (?:disponible|publicado)|the advertiser removed it|el anunciante lo ha eliminado",
                    low,
                )
            ),
            "sold_notice": bool(
                re.search(
                    r"this (?:property|listing) has been sold|este inmueble se ha vendido",
                    low,
                )
            ),
            "reserved_notice": bool(
                re.search(r"este inmueble (?:está|esta) reservado", low)
            ),
        }


def read_archive(path, expected):
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ReaderFailure("not_the_listing_page")
    with path.open("rb") as archive:
        raw = archive.read(MAX_ARCHIVE_BYTES + 1)
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ReaderFailure("not_the_listing_page")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if any(part.defects for part in message.walk()):
        raise ArchiveIncomplete("archive_incomplete")
    if message.get("Snapshot-Content-Location") != expected:
        raise ReaderFailure("not_the_listing_page")
    parts = [
        p
        for p in message.walk()
        if p.get_content_type() == "text/html" and p.get("Content-Location") == expected
    ]
    if len(parts) != 1:
        raise ReaderFailure("not_the_listing_page")
    parser = PublicFields()
    parser.feed(
        parts[0]
        .get_payload(decode=True)
        .decode(parts[0].get_content_charset() or "utf-8")
    )
    parser.close()
    return dict(parser.result(), archive_url=expected, main_url=expected, saved=True)


class ReaderFailure(RuntimeError):
    pass


class ArchiveIncomplete(ReaderFailure):
    pass


def apple(script, deadline):
    """Every AppleEvent has its own killable, bounded process group."""
    budget = min(4, deadline - time.monotonic())
    if budget <= 0:
        raise ReaderFailure("browser_unavailable")
    wrapped = "with timeout of 3 seconds\n" + script + "\nend timeout\n"
    proc = subprocess.Popen(
        [OSASCRIPT, "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, error = proc.communicate(wrapped, timeout=budget)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=1)
        raise
    if proc.returncode:
        reason = (
            "missing_configuration"
            if any(
                s in error.lower()
                for s in ("-1743", "turned off", "not authorized", "not permitted")
            )
            else "browser_unavailable"
        )
        raise ReaderFailure(reason)
    return output.strip()


class OwnedTab:
    def __init__(self, deadline):
        self.deadline = deadline
        self.window = self.tab = None
        self.last_url = "about:blank"

    def command(self, body, *, deadline=None):
        prefix = f'''tell application "Google Chrome"
set auditTab to missing value
repeat with n from 1 to (count of tabs of window id {self.window})
if ((id of tab n of window id {self.window}) as string) is "{self.tab}" then set auditTab to tab n of window id {self.window}
end repeat
if auditTab is missing value then error "Owned tab absent"
'''
        return apple(
            prefix + body + "\nend tell",
            deadline if deadline is not None else self.deadline,
        )

    def open(self):
        raw = apple(
            """tell application "Google Chrome"
set auditWindow to make new window
set URL of active tab of auditWindow to "about:blank"
return (id of auditWindow as string) & ":" & (id of active tab of auditWindow as string)
end tell""",
            self.deadline,
        )
        window, tab = raw.split(":")
        if not window.isdigit() or not tab.isdigit():
            raise ReaderFailure("browser_unavailable")
        self.window, self.tab = int(window), int(tab)

    def navigate(self, url):
        # Bind intent before the AppleEvent: it can apply and then time out.
        self.last_url = url
        self.command("set URL of auditTab to " + json.dumps(url))

    def location(self):
        return self.command("return URL of auditTab")

    def archive(self, url, path):
        # Guard every read/save. An owner-repurposed tab is left alone.
        if self.location() != url:
            raise ReaderFailure("not_the_listing_page")
        if self.command("return loading of auditTab") != "false":
            return False
        self.command(
            "save auditTab in (POSIX file "
            + json.dumps(str(path))
            + ') as "single file"'
        )
        if self.location() != url:
            raise ReaderFailure("not_the_listing_page")
        return True

    def close(self):
        if self.tab is not None:
            allowed = {"about:blank", self.last_url}
            # A query-stripping redirect stays on the requested listing.
            base = self.last_url.partition("?")[0]
            try:
                identity(base)
                allowed.add(base)
            except ValueError:
                pass
            # Unknown destinations could be the owner's navigation; preserve
            # them along with every additional tab in this created window.
            self.command(
                "if {"
                + ", ".join(json.dumps(url) for url in sorted(allowed))
                + "} contains (URL of auditTab) then close auditTab",
                deadline=time.monotonic() + CLEANUP_SECONDS,
            )


def read_native(url, nonce):
    wanted = navigation_url(url, nonce)
    if socket.gethostname() != MACBOOK:
        raise ReaderFailure("missing_configuration")
    if subprocess.run(
        ["/usr/bin/pgrep", "-x", "Google Chrome"], stdout=subprocess.DEVNULL, timeout=2
    ).returncode:
        raise ReaderFailure("missing_configuration")
    folder = Path.home() / "Library/Caches/IdealistaRank/native-status"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + REMOTE_SECONDS
    tab = OwnedTab(deadline)
    with (folder / "reader.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"nonce": nonce, "refusal": "browser_busy"}

        def expired(signum, frame):
            raise ReaderFailure("browser_unavailable")

        old_handlers = {
            sig: signal.getsignal(sig)
            for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        }
        for sig in old_handlers:
            signal.signal(sig, expired)
        signal.setitimer(signal.ITIMER_REAL, REMOTE_SECONDS)
        try:
            with tempfile.TemporaryDirectory(dir=folder) as temporary:
                path = Path(temporary) / "listing.mhtml"
                tab.open()
                tab.navigate(wanted)
                while time.monotonic() < deadline:
                    location = tab.location()
                    if (
                        location == wanted
                        and tab.command("return loading of auditTab") == "false"
                    ):
                        if tab.archive(wanted, path):
                            break
                    elif location != "about:blank" and location != wanted:
                        raise ReaderFailure("not_the_listing_page")
                    time.sleep(0.2)
                else:
                    raise ReaderFailure("browser_unavailable")
                previous = None
                while time.monotonic() < deadline:
                    if tab.location() != wanted:
                        raise ReaderFailure("not_the_listing_page")
                    size = path.stat().st_size if path.exists() else 0
                    if size > MAX_ARCHIVE_BYTES:
                        raise ReaderFailure("not_the_listing_page")
                    if size and size == previous:
                        try:
                            page = read_archive(path, wanted)
                            return dict(
                                page,
                                nonce=nonce,
                                url=wanted,
                                ready="complete",
                                reader_elapsed_ms=(time.monotonic() - started) * 1000,
                            )
                        except ArchiveIncomplete:
                            # Only unfinished MIME can become valid by waiting.
                            pass
                        except (ValueError, UnicodeError, LookupError):
                            raise ReaderFailure("not_the_listing_page") from None
                    previous = size
                    time.sleep(0.2)
                raise ReaderFailure("browser_unavailable")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            try:
                tab.close()
            except (ReaderFailure, subprocess.SubprocessError, ValueError):
                pass
            finally:
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)


def main(payload):
    nonce = payload.get("nonce")
    try:
        validate_request(payload.get("url"), nonce)
        result = read_native(payload["url"], nonce)
    except (
        ReaderFailure,
        subprocess.SubprocessError,
        OSError,
        ValueError,
        LookupError,
    ) as exc:
        result = {
            "nonce": nonce,
            "refusal": str(exc)
            if isinstance(exc, ReaderFailure)
            else "browser_unavailable",
        }
    print(json.dumps(result), flush=True)


def cli():
    if len(sys.argv) != 4 or sys.argv[1] not in {"--emit", "--read"}:
        raise SystemExit("expected --emit|--read URL NONCE")
    payload = dict(url=sys.argv[2], nonce=sys.argv[3])
    validate_request(**payload)
    if sys.argv[1] == "--emit":
        print(Path(__file__).read_text())
        print("\nmain(json.loads(" + repr(json.dumps(payload)) + "))")
    else:
        main(payload)


if __name__ == "__main__" and len(sys.argv) > 1:
    cli()
