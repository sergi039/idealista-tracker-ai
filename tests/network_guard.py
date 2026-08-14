"""Refuse the live internet for the duration of a test run (issue #307).

CLAUDE.md requires external APIs to be mocked in tests. Nothing enforced it,
and an unmocked call is invisible from the outside: every caller in this
codebase handles a failed request. `utils/geocoding.py` falls back from Google
to Nominatim and then swallows the failure (utils/geocoding.py:71,
utils/geocoding.py:109); a refused Overpass lookup only *degrades* an
enrichment run, because Overpass is the advisory source there and no score
reads it (#153 -- a refused Google call is decisive and does fail the run, so
this hides the free APIs, not the paid ones). So a test that reaches the public internet with the fake test
API key reaches the same verdict as one that mocks the transport, and pays for
the round trip on every run, forever, while proving nothing about the code.

PR #306 found one of these the hard way: the sea-view step it wired into
ingestion reached live Overpass from three suites, and in the pre-push gate's
sandbox -- where outbound connects are dropped rather than refused -- those
connects sat in SYN_SENT and stalled the gate for tens of minutes. Nothing in
the run said the word "network". That is the failure mode this guard exists to
make impossible: an unmocked call now fails on the line that makes it, at the
moment it is written.

Every blocked attempt is *recorded* as well as refused, because refusing is not
enough on its own. The exception is raised inside code that catches
`Exception` and degrades, so the test that made the call can still pass; the
recorded attempt is what survives that, and `tests/conftest.py` turns the
record into a session-level failure naming every test that reached out.

What is allowed, and why:

* loopback -- 127.0.0.0/8, ::1, `localhost`, the unspecified address.
  tests/test_ai_bridge_isolation.py and tests/test_ai_bridge_schema.py serve
  the AI bridge from a `ThreadingHTTPServer` on 127.0.0.1:0 and drive it with
  real HTTP requests, and one of them spawns the bridge as a process and polls
  it on a free loopback port.
* every address family that is not AF_INET/AF_INET6 -- an AF_UNIX socket
  cannot leave the machine, and macOS uses other families for local plumbing.

Everything else is refused, with no per-host allowlist, because the suite has
nothing else it legitimately reaches. In particular this guard has no opinion
about the CI PostgreSQL service, and could not have one: psycopg2 connects
through libpq, in C, which never touches Python's socket module. Measured
2026-08-14 -- a `psycopg2.connect` to a blackholed address under this guard
spent its full `connect_timeout` in libpq and the guard never saw it. The same
goes for anything else that dials from C. What the guard covers is what the
leaks were made of: `requests`, `urllib`, `http.client`, `imaplib`, and every
`ssl`-wrapped socket built on them -- all four verified refused, by name, the
same day.

Nor does it reach a subprocess: that is a separate interpreter, so the tests
that spawn one (tests/test_ai_bridge_isolation.py, tests/test_merge_bot_dry_run.py,
tests/test_local_ci_hook.py, and the autopilot shell harnesses that run `nc`
and `curl` against 127.0.0.1) are not covered here at all. They stub their
binaries instead; this guard neither helps them nor claims to.

`PYTEST_ALLOW_NETWORK=1` turns the whole thing off, for the case where someone
deliberately wants to reproduce a live-API investigation. It is not a way to
make a red run green: a suite that needs live services is skipped, never
passed (CLAUDE.md).
"""

from __future__ import annotations

import ipaddress
import os
import socket
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DISABLE_ENV = "PYTEST_ALLOW_NETWORK"

REPO_ROOT = Path(__file__).resolve().parent.parent

# Address families that can reach another machine. Everything else -- AF_UNIX,
# AF_NETLINK, macOS's AF_SYSTEM -- is local plumbing by construction.
ROUTABLE_FAMILIES = (socket.AF_INET, socket.AF_INET6)

# How many frames of this repository to name in a refusal. Three reaches from
# the transport helper, through the service that called it, to the test.
CALLER_FRAMES = 3


class NetworkAccessDuringTest(RuntimeError):
    """Raised in place of a connect to anything that is not this machine.

    A `RuntimeError` rather than an `OSError`, for two reasons that were
    checked rather than assumed. urllib3 catches `OSError` out of a connect and
    re-raises it as `NewConnectionError`, which `requests` then wraps again --
    the guard's message, the part that says which line to fix, would arrive
    buried in two layers of somebody else's exception. And `utils/http.py`
    retries `requests.RequestException` up to three times with a backoff
    between them, so an OSError-shaped refusal would cost three refusals and
    two sleeps. (urllib3's own retrying is not the problem: requests' default
    adapter is `Retry(total=0)` and nothing here mounts another.) A
    `RuntimeError` is caught by none of that, so one refusal stays one refusal
    and reaches the caller intact.
    """


@dataclass(frozen=True)
class Attempt:
    """One refused connect, kept whether or not its caller re-raised."""

    nodeid: str
    destination: str
    caller: str


_attempts: list[Attempt] = []
_current_nodeid = "<collection>"
_originals: dict[str, object] = {}


def _is_local(host: object) -> bool:
    """True for a target that cannot leave this machine.

    `None` and the empty string are how "this host" is spelled to
    `getaddrinfo` and `connect`; a scope id (`fe80::1%lo0`) and the brackets a
    URL leaves on an IPv6 literal are stripped before parsing; an IPv4-mapped
    IPv6 address (`::ffff:127.0.0.1`) is judged on the address it maps, which
    `IPv6Address.is_loopback` does not do.
    """
    if host is None:
        return True
    if not isinstance(host, (str, bytes)):
        return False
    name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
    name = name.strip()
    if name in {"", "localhost"} or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name.strip("[]").split("%")[0])
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback or address.is_unspecified


def _caller() -> str:
    """The lines of *this* repository that led to the connect, innermost first.

    Reporting the frame that raised would name urllib3, which is never the
    thing to fix, so frames inside .venv/site-packages and inside this module
    are skipped. Reporting only the innermost repository frame is not enough
    either: nearly every request in this codebase goes through
    `request_with_retries`, so one frame names utils/http.py for every leak
    there has ever been. The frame above it is the one that identifies the
    call -- `utils/geocoding.py:84` is the difference between "something made a
    request" and "the Nominatim fallback did".
    """
    chain: list[str] = []
    for frame in reversed(traceback.extract_stack()):
        # `<frozen runpy>`, `<string>`, `<stdin>`: not paths. Path.resolve()
        # would hang them off the current directory, which during a run is the
        # repository root, and they would be reported as repository frames.
        if frame.filename.startswith("<"):
            continue
        path = Path(frame.filename)
        if path == Path(__file__):
            continue
        try:
            relative = path.resolve().relative_to(REPO_ROOT)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in {".venv", "site-packages"}:
            continue
        chain.append(f"{relative}:{frame.lineno}")
        if len(chain) == CALLER_FRAMES:
            break
    return " <- ".join(chain) if chain else "<outside this repository>"


def _describe(host: object, port: object) -> str:
    text = host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)
    if port in (None, ""):
        return text
    # An IPv6 literal needs its brackets: `2001:db8::1:443` is not "that host,
    # that port", it is a valid and *different* address, so a report without
    # them names a destination nobody dialled.
    return f"[{text}]:{port}" if ":" in text else f"{text}:{port}"


def _refuse(destination: str) -> None:
    """Record the attempt, then raise. Both halves matter -- see the module
    docstring: the raise stops the call, the record survives a caller that
    catches `Exception` and degrades."""
    caller = _caller()
    _attempts.append(
        Attempt(nodeid=_current_nodeid, destination=destination, caller=caller)
    )
    raise NetworkAccessDuringTest(
        f"test tried to connect to {destination} (from {caller}). External "
        "APIs must be mocked in tests -- patch the transport this call goes "
        f"through. Run with {DISABLE_ENV}=1 to allow live calls (issue #307)."
    )


def _check(sock: socket.socket, address: object) -> None:
    if getattr(sock, "family", None) not in ROUTABLE_FAMILIES:
        return
    host, port = (
        (address[0], address[1])
        if isinstance(address, (tuple, list)) and len(address) >= 2
        else (address, None)
    )
    if _is_local(host):
        return
    _refuse(_describe(host, port))


def _guarded_connect(sock, address):
    _check(sock, address)
    return _originals["connect"](sock, address)


def _guarded_connect_ex(sock, address):
    _check(sock, address)
    return _originals["connect_ex"](sock, address)


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    """Refuse the *lookup* of a host this run may not reach.

    Blocking here as well as at connect() is what puts the hostname in the
    message -- `socket.create_connection` and urllib3 both resolve first and
    connect to an IP, so the connect guard alone would report
    `142.250.185.10:443` and leave the reader to guess whose it is. It also
    means a sandboxed run never waits on a DNS server it cannot reach.
    """
    if not _is_local(host):
        _refuse(_describe(host, port))
    return _originals["getaddrinfo"](host, port, *args, **kwargs)


def install() -> bool:
    """Patch the connect path. Returns False when the guard is switched off."""
    if _originals or os.environ.get(DISABLE_ENV, "").strip() not in {"", "0"}:
        return False
    _originals.update(
        connect=socket.socket.connect,
        connect_ex=socket.socket.connect_ex,
        getaddrinfo=socket.getaddrinfo,
    )
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.getaddrinfo = _guarded_getaddrinfo
    return True


def installed() -> bool:
    """Whether the connect path is currently patched.

    The guard's own tests assert on refusals, so they must skip -- never
    silently pass -- when someone has switched it off with the escape hatch.
    """
    return bool(_originals)


def uninstall() -> None:
    if not _originals:
        return
    socket.socket.connect = _originals["connect"]
    socket.socket.connect_ex = _originals["connect_ex"]
    socket.getaddrinfo = _originals["getaddrinfo"]
    _originals.clear()


def note_test(nodeid: str) -> None:
    """Attribute later attempts to `nodeid` (called from the runtest hooks)."""
    global _current_nodeid
    _current_nodeid = nodeid


def attempts() -> list[Attempt]:
    return list(_attempts)


@contextmanager
def capture_attempts():
    """Collect the attempts made in this block, and leave the session record
    as it was. For the guard's own tests, which refuse connects on purpose and
    must not be reported as leaks by the run they are part of."""
    global _attempts
    outer, _attempts = _attempts, []
    try:
        yield _attempts
    finally:
        _attempts = outer


def summary_lines() -> list[str]:
    """The session report: one block per test that reached for the network."""
    if not _attempts:
        return []
    by_test: dict[str, list[Attempt]] = {}
    for attempt in _attempts:
        by_test.setdefault(attempt.nodeid, []).append(attempt)

    lines = [
        f"{len(_attempts)} network call(s) were refused, from {len(by_test)} test(s):",
        "",
    ]
    for nodeid, attempts_for_test in by_test.items():
        lines.append(nodeid)
        seen: set[tuple[str, str]] = set()
        for attempt in attempts_for_test:
            key = (attempt.destination, attempt.caller)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  -> {attempt.destination}  ({attempt.caller})")
    lines.extend(
        [
            "",
            "Each of these reached the live internet before this guard existed "
            "and is not tested by the assertion that follows it: the caller "
            "catches the failure and degrades. Mock the transport (CLAUDE.md), "
            "or skip the test where the service is genuinely required.",
        ]
    )
    return lines
