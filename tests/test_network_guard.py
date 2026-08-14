"""Issue #307: the suite may not reach the live internet.

The guard under test is tests/network_guard.py, installed for the whole
session from tests/conftest.py. These tests are what stops it from being
quietly weakened: the two halves that matter are that a public destination is
refused *and named*, and that the local plumbing the suite genuinely uses --
loopback TCP, unix sockets, an allowlisted database host -- still works.

Every refusal here is deliberate, so each one runs inside
`network_guard.capture_attempts()`: without it this file would appear in the
session's own leak report as eleven tests that reached for the network.

Nothing in this file connects anywhere. The public addresses below are
documentation ranges (RFC 5737 / RFC 3849) and the guard raises before the
real connect, so the assertions hold on a machine with no network at all.
"""

from __future__ import annotations

import socket

import pytest
import requests

from tests import network_guard
from tests.network_guard import NetworkAccessDuringTest

pytestmark = pytest.mark.skipif(
    not network_guard.installed(),
    reason=f"the network guard is switched off ({network_guard.DISABLE_ENV})",
)

# RFC 5737 TEST-NET-1 and RFC 3849 documentation prefix: routable-looking,
# reserved for documentation, and never actually dialled by these tests.
PUBLIC_V4 = "192.0.2.10"
PUBLIC_V6 = "2001:db8::1"


class TestTheInternetIsRefused:
    def test_a_connect_to_a_public_address_raises_and_names_it(self):
        with network_guard.capture_attempts() as attempts:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                with pytest.raises(NetworkAccessDuringTest) as excinfo:
                    sock.connect((PUBLIC_V4, 443))

        message = str(excinfo.value)
        assert f"{PUBLIC_V4}:443" in message, (
            "the refusal must name the destination, or a reader cannot tell "
            f"which call to mock: {message}"
        )
        assert len(attempts) == 1
        assert attempts[0].caller.startswith("tests/test_network_guard.py:"), (
            "the refusal must name the line in this repository that asked for "
            f"the connect, not a frame inside urllib3: {attempts[0].caller}"
        )

    def test_an_ipv6_connect_is_refused_too(self):
        with network_guard.capture_attempts():
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
                with pytest.raises(NetworkAccessDuringTest) as excinfo:
                    sock.connect((PUBLIC_V6, 443, 0, 0))
        assert PUBLIC_V6 in str(excinfo.value)

    def test_connect_ex_is_refused_as_well(self):
        """`connect_ex` reports failure by return value rather than raising,
        so a guard that patched only `connect` would let it through silently."""
        with network_guard.capture_attempts():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                with pytest.raises(NetworkAccessDuringTest):
                    sock.connect_ex((PUBLIC_V4, 443))

    def test_a_name_lookup_is_refused_before_any_dns_traffic(self):
        """Blocking the lookup is what puts a *hostname* in the message, and
        what keeps a sandboxed run from waiting on a resolver it cannot
        reach."""
        with network_guard.capture_attempts():
            with pytest.raises(NetworkAccessDuringTest) as excinfo:
                socket.getaddrinfo("maps.googleapis.com", 443)
        assert "maps.googleapis.com" in str(excinfo.value)

    def test_the_transport_the_application_actually_uses_is_covered(self):
        """The end-to-end shape of every leak this guard exists for: a plain
        `requests.get`, the call `utils/geocoding.py` and the enrichment
        services make, through urllib3's own connection pool."""
        with network_guard.capture_attempts() as attempts:
            with pytest.raises(NetworkAccessDuringTest):
                requests.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": "Oviedo"},
                    timeout=5,
                )
        assert any("maps.googleapis.com" in a.destination for a in attempts)


class TestWhatTheSuiteLegitimatelyUsesStillWorks:
    def test_a_real_loopback_connection_succeeds(self):
        """tests/test_ai_bridge_isolation.py starts a bridge process on a free
        loopback port and polls it over HTTP; the CI PostgreSQL service is
        reached at 127.0.0.1:5432. Both would die with the guard's
        exception if loopback were not exempt, so this connects for real."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(server.getsockname())
                accepted, _ = server.accept()
                accepted.close()

    def test_localhost_still_resolves(self):
        assert socket.getaddrinfo("localhost", 0)

    def test_a_unix_socket_is_not_the_internet(self, tmp_path):
        """An AF_UNIX connect cannot leave the machine whatever its path says,
        so it must fail as the operating system would -- not as a leak."""
        missing = tmp_path / "there-is-no-server-here.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            with pytest.raises(OSError) as excinfo:
                sock.connect(str(missing))
        assert not isinstance(excinfo.value, NetworkAccessDuringTest)

    def test_the_escape_hatch_really_switches_the_guard_off(self, monkeypatch):
        """The message on every refusal points at this variable, so it has to
        work -- and it has to work by *not installing*, not by allowing one
        call through, or a run that opted out would still pay for the patch.

        The session's own guard is dropped and restored around the check; the
        `finally` is what keeps a failure here from leaving the rest of the run
        unguarded.
        """
        network_guard.uninstall()
        try:
            monkeypatch.setenv(network_guard.DISABLE_ENV, "1")
            assert network_guard.install() is False
            assert not network_guard.installed()
            assert socket.socket.connect is not network_guard._guarded_connect
        finally:
            monkeypatch.delenv(network_guard.DISABLE_ENV, raising=False)
            network_guard.install()
        assert network_guard.installed()


class TestWhichAddressesCountAsThisMachine:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.53",
            "::1",
            # IPv4-mapped loopback: `IPv6Address.is_loopback` is False for it,
            # so this only passes because the guard maps it back first.
            "::ffff:127.0.0.1",
            "0.0.0.0",
            "::",
            "localhost",
            "",
            None,
        ],
    )
    def test_local_targets_are_allowed(self, host):
        assert network_guard._is_local(host)

    @pytest.mark.parametrize(
        "host",
        [
            PUBLIC_V4,
            PUBLIC_V6,
            "8.8.8.8",
            "maps.googleapis.com",
            "overpass-api.de",
            # Not loopback: a link-local address is another machine on the LAN.
            "fe80::1%lo0",
            "169.254.169.254",
        ],
    )
    def test_everything_else_is_not(self, host):
        assert not network_guard._is_local(host)


class TestASwallowedRefusalIsStillReported:
    def test_the_attempt_outlives_a_caller_that_degrades(self):
        """The reason the guard records as well as raises. `utils/geocoding.py`
        catches `Exception` and falls back (utils/geocoding.py:71), so a
        refused call leaves a green test and no trace in the output -- the
        record is what survives, and the session report is what prints it."""
        with network_guard.capture_attempts() as attempts:
            try:
                socket.create_connection((PUBLIC_V4, 80), timeout=1)
            except Exception:  # noqa: BLE001 - the swallowing caller, staged
                pass

            assert attempts, "a swallowed refusal left no record"
            report = "\n".join(network_guard.summary_lines())

        assert PUBLIC_V4 in report
        assert "test_the_attempt_outlives_a_caller_that_degrades" in report, (
            "the report must name the test that reached out"
        )

    def test_the_report_is_empty_when_nothing_reached_out(self):
        with network_guard.capture_attempts():
            assert network_guard.summary_lines() == []
