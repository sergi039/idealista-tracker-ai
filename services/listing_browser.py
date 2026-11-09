"""Read public Idealista fields in an owned normal-Chrome tab on the MacBook.

Stdlib-only source is sent over SSH stdin. Chrome's supported MHTML save does
not need JavaScript through Apple Events. Archives never leave the MacBook.
"""

from __future__ import annotations

from contextlib import ExitStack
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
REMOVED_NOTICE = re.compile(
    r"(?:this listing is no longer (?:published|available)|este (?:inmueble|anuncio) ya no está (?:disponible|publicado)|the advertiser removed it|el anunciante lo ha eliminado)[.!]?",
    re.I,
)
SOLD_NOTICE = re.compile(
    r"(?:this (?:property|listing) has been sold|este inmueble se ha vendido)[.!]?",
    re.I,
)
CHALLENGE_TITLE = re.compile(
    r"(?:just a moment|checking your browser|verify you are human|please enable js|ddos-guard)[\s.!…]*",
    re.I,
)


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
        h1, price = page.get("h1"), page.get("price")
        if (
            not isinstance(h1, str)
            or not isinstance(price, str)
            or len(h1) > 1000
            or len(price) > 100
        ):
            return bad
        # A seller's description is not a status banner. Retained live title
        # or price contradicts a terminal notice; never hide that listing.
        if page.get("removed_notice") or page.get("sold_notice"):
            notice = REMOVED_NOTICE if page.get("removed_notice") else SOLD_NOTICE
            if (
                price.strip()
                or (h1.strip() and not notice.fullmatch(h1.strip()))
                or (page.get("removed_notice") and page.get("sold_notice"))
            ):
                return "error", "availability_ambiguous"
            if page.get("reference") == identifier:
                return ("removed" if page.get("removed_notice") else "sold"), None
            return "error", "availability_ambiguous"
        if page.get("reserved_notice"):
            return "error", "availability_ambiguous"
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
        self.page_title = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "iframe"} and re.match(
            r"(?:https?:)?//(?:[a-z0-9-]+\.)?captcha-delivery\.com/",
            (attrs.get("src") or "").strip(),
            re.I,
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
        if any(tag == "head" for tag, *_ in self.stack) and any(
            tag == "title" for tag, *_ in self.stack
        ):
            self.page_title.append(value)
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
        if self.challenge or CHALLENGE_TITLE.fullmatch(
            "".join(self.page_title).strip()
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
            "blocked": False,
            "removed_notice": bool(REMOVED_NOTICE.search(text)),
            "sold_notice": bool(SOLD_NOTICE.search(text)),
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


CHROME_EXECUTABLE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Use Apple's local process-addressed events, not an application name or bundle
# address. A second Chrome (e.g. Playwright) shares both of those names.
CHROME_EVENTS = r"""
ObjC.import("AppKit");
const AE = $.NSAppleEventDescriptor;
const code = s => [...s].reduce((n, c) => n * 256 + c.charCodeAt(0), 0);
const text = s => AE.descriptorWithString(s);
const type = s => AE.descriptorWithTypeCode(code(s));
const enumeration = s => AE.descriptorWithEnumCode(code(s));
function record(fields) {
    const r = AE.recordDescriptor;
    for (const k in fields) r.setDescriptorForKeyword(fields[k], code(k));
    return r;
}
function spec(want, form, key, from) {
    return record({want: type(want), form: enumeration(form), seld: key,
                   from: from || AE.nullDescriptor}).coerceToDescriptorType(code("obj "));
}
const property = (k, from) => spec("prop", "prop", type(k), from);
const byID = (kind, id, from) =>
    spec(kind, "ID  ", AE.descriptorWithInt32(id), from);
function matching(kind, key, value, from) {
    const examined = AE.descriptorWithDescriptorTypeBytesLength(code("exmn"), null, 0);
    const cmp = record({relo: enumeration("=   "), obj1: property(key, examined),
                        obj2: text(value)}).coerceToDescriptorType(code("cmpd"));
    return spec(kind, "test", cmp, from);
}
function chromeIdentity(app, executable) {
    if (app.isNil() || app.terminated ||
        ObjC.unwrap(app.bundleIdentifier) !== "com.google.Chrome" ||
        ObjC.unwrap(app.executableURL.path) !== executable || app.launchDate.isNil()) {
        return null;
    }
    return {pid: Number(app.processIdentifier),
            started: Number(app.launchDate.timeIntervalSinceReferenceDate)};
}
function run(args) {
    const req = JSON.parse(args[0]);
    const running = $.NSRunningApplication;
    if (req.operation === "discover") {
        const apps = ObjC.unwrap(running.runningApplicationsWithBundleIdentifier("com.google.Chrome"));
        return JSON.stringify(apps.map(a => chromeIdentity(a, req.executable)).filter(Boolean));
    }
    const target = AE.descriptorWithProcessIdentifier(req.target.pid);
    function send(id, fields) {
        // Recheck the launch identity even between events in a single operation.
        // A vanished Chrome must not be relaunched or substituted by another.
        const app = chromeIdentity(running.runningApplicationWithProcessIdentifier(req.target.pid),
                                   req.executable);
        if (!app || app.started !== req.target.started) throw Error("Chrome process changed");
        const e = AE.appleEventWithEventClassEventIDTargetDescriptorReturnIDTransactionID(
            code("core"), code(id), target, -1, 0);
        for (const k in fields) e.setParamDescriptorForKeyword(fields[k], code(k));
        const error = Ref();
        const reply = e.sendEventWithOptionsTimeoutError(3, 3, error); // wait for reply
        if (reply.isNil()) throw Error("AppleEvent " + error[0].code);
        const failure = reply.paramDescriptorForKeyword(code("errn"));
        if (!failure.isNil() && failure.int32Value) throw Error("AppleEvent " + failure.int32Value);
        return reply.paramDescriptorForKeyword(code("----"));
    }
    const get = (key, obj) => send("getd", {"----": property(key, obj)});
    const url = t => ObjC.unwrap(get("URL ", t).stringValue);
    const ids = (w, t) => [ObjC.unwrap(get("ID  ", w).stringValue),
                           ObjC.unwrap(get("ID  ", t).stringValue)];
    const setURL = (t, dest) => send("setd", {"----": property("URL ", t), data: text(dest)});
    let result = null;
    if (req.operation === "open") {
        const w = send("crel", {kocl: type("cwin"),
                               prdt: record({GNam: text(req.marker), mode: text("normal")})});
        const t = get("acTa", w);
        setURL(t, req.initial);
        result = ids(w, t);
    } else if (req.operation === "recover") {
        const windows = send("getd", {"----": matching("cwin", "GNam", req.marker)});
        if (Number(windows.numberOfItems) !== 1) return "null";
        const w = windows.descriptorAtIndex(1);
        const tabs = send("getd", {"----": matching("CrTb", "URL ", req.initial, w)});
        let t;
        if (Number(tabs.numberOfItems) === 1) {
            t = tabs.descriptorAtIndex(1);
        } else {
            const count = send("cnte", {"----": w, kocl: type("CrTb")});
            if (Number(count.int32Value) !== 1) return "null";
            t = get("acTa", w);
            if (!["about:blank", "chrome://newtab/"].includes(url(t))) return "null";
        }
        result = ids(w, t);
    } else {
        const tab = byID("CrTb", req.tab, byID("cwin", req.window));
        switch (req.operation) {
        case "navigate": setURL(tab, req.url); break;
        case "location": result = url(tab); break;
        case "loading": result = get("ldng", tab).booleanValue; break;
        case "save":
            if (url(tab) !== req.url) return "false";
            send("save", {"----": tab, kfil: AE.descriptorWithFileURL($.NSURL.fileURLWithPath(req.path)),
                          fltp: text("single file")});
            result = true;
            break;
        case "close":
            if (req.allowed.includes(url(tab))) send("clos", {"----": tab});
            break;
        default: throw Error("Unknown Chrome operation");
        }
    }
    return JSON.stringify(result);
}
"""


def apple(operation, deadline, **fields):
    """Every operation has its own killable, bounded process group."""
    budget = min(4, deadline - time.monotonic())
    if budget <= 0:
        raise ReaderFailure("browser_unavailable")
    request = dict(operation=operation, executable=CHROME_EXECUTABLE, **fields)
    proc = subprocess.Popen(
        [OSASCRIPT, "-l", "JavaScript", "-", json.dumps(request)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, error = proc.communicate(CHROME_EVENTS, timeout=budget)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=1)
        raise
    if proc.returncode:
        reason = "missing_configuration" if "-1743" in error else "browser_unavailable"
        raise ReaderFailure(reason)
    return json.loads(output)


def select_chrome(deadline):
    """Select the single stock Chrome process, never one launched with flags."""
    candidates = apple("discover", deadline)
    ordinary = []
    for target in candidates:
        pid = target["pid"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReaderFailure("browser_unavailable")
        process = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "uid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=min(2, remaining),
        )
        if process.returncode == 0 and process.stdout.strip().split(maxsplit=1) == [
            str(os.getuid()),
            CHROME_EXECUTABLE,
        ]:
            ordinary.append(target)
    if len(ordinary) != 1:
        raise ReaderFailure("missing_configuration")
    return ordinary[0]


class OwnedTab:
    def __init__(self, deadline):
        self.deadline = deadline
        self.target = self.window = self.tab = None
        self.creation_attempted = False
        self.last_url = "about:blank"
        self.marker = "IdealistaRank status " + os.urandom(16).hex()
        self.initial_url = "about:blank#" + self.marker.replace(" ", "-")

    def command(self, operation, *, deadline=None, **fields):
        return apple(
            operation,
            deadline if deadline is not None else self.deadline,
            target=self.target,
            window=self.window,
            tab=self.tab,
            **fields,
        )

    def set_ids(self, ids):
        if (
            not isinstance(ids, list)
            or len(ids) != 2
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[0-9]{1,10}", value)
                or not 0 < int(value) < 2**31
                for value in ids
            )
        ):
            raise ReaderFailure("browser_unavailable")
        self.window, self.tab = map(int, ids)

    def open(self):
        self.target = select_chrome(self.deadline)
        self.creation_attempted = True
        self.set_ids(self.command("open", marker=self.marker, initial=self.initial_url))

    def recover_initial_tab(self, deadline):
        # A lost reply can only be recovered in the original Chrome process.
        ids = self.command(
            "recover", deadline=deadline, marker=self.marker, initial=self.initial_url
        )
        if ids is not None:
            self.set_ids(ids)

    def navigate(self, url):
        # Bind intent before the AppleEvent: it can apply and then time out.
        self.last_url = url
        self.command("navigate", url=url)

    def location(self):
        return self.command("location")

    def loading(self):
        return self.command("loading")

    def archive(self, url, path):
        if self.location() != url:
            raise ReaderFailure("not_the_listing_page")
        if self.loading():
            return False
        if not self.command("save", url=url, path=str(path)) or self.location() != url:
            raise ReaderFailure("not_the_listing_page")
        return True

    def close(self):
        deadline = time.monotonic() + CLEANUP_SECONDS
        if self.tab is None:
            if not self.creation_attempted:
                return
            self.recover_initial_tab(deadline)
        if self.tab is not None:
            allowed = {
                "about:blank",
                "chrome://newtab/",
                self.initial_url,
                self.last_url,
            }
            base = self.last_url.partition("?")[0]
            try:
                identity(base)
                allowed.add(base)
            except ValueError:
                pass
            # A different destination might be the owner's navigation.
            self.command("close", deadline=deadline, allowed=sorted(allowed))


def read_native(url, nonce):
    wanted = navigation_url(url, nonce)
    if socket.gethostname() != MACBOOK:
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

        cleaning_up = False
        deferred_signals = set()

        def expired(signum, frame):
            nonlocal cleaning_up
            # A process-directed signal can reach an unblocked background
            # thread, then Python dispatches it on this main thread anyway.
            if cleaning_up:
                deferred_signals.add(signum)
                return
            cleaning_up = True
            raise ReaderFailure("browser_unavailable")

        old_handlers = {
            sig: signal.getsignal(sig)
            for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        }
        masks = []

        def block_cleanup_signals():
            nonlocal cleaning_up
            cleaning_up = True
            masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, old_handlers))

        def restore_signals():
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            if masks:
                signal.pthread_sigmask(signal.SIG_SETMASK, masks[0])
            for sig in tuple(deferred_signals):
                signal.raise_signal(sig)

        def close_owned_tab():
            try:
                tab.close()
            except (ReaderFailure, subprocess.SubprocessError, ValueError):
                pass

        # LIFO: block signals, disarm timer, close our tab, delete archive,
        # restore handlers/mask. ExitStack runs remaining cleanup callbacks
        # even if an earlier one is interrupted or fails.
        with ExitStack() as cleanup:
            cleanup.callback(restore_signals)
            temporary = cleanup.enter_context(tempfile.TemporaryDirectory(dir=folder))
            cleanup.callback(close_owned_tab)
            cleanup.callback(signal.setitimer, signal.ITIMER_REAL, 0)
            cleanup.callback(block_cleanup_signals)
            try:
                for sig in old_handlers:
                    signal.signal(sig, expired)
                signal.setitimer(signal.ITIMER_REAL, REMOTE_SECONDS)
                path = Path(temporary) / "listing.mhtml"
                tab.open()
                tab.navigate(wanted)
                while time.monotonic() < deadline:
                    location = tab.location()
                    if location == wanted and not tab.loading():
                        if tab.archive(wanted, path):
                            break
                    elif (
                        location not in ("about:blank", "chrome://newtab/", wanted)
                        and location != tab.initial_url
                    ):
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
                # Protect ExitStack's own bookkeeping as well as callbacks.
                cleaning_up = True


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
