"""One host's wall is not every host's.

The refusal breaker was a single process-wide object, which was right while
every listing was on idealista.com. idealista refuses this machine
permanently -- measured 2026-08-15 over 76 consecutive properties, every one a
DataDome block -- so its breaker is open essentially all the time. With one
shared breaker that did not *degrade* a fotocasa check, it forbade it: three
idealista refusals, which arrive the moment anybody presses anything, and every
later check on any host answered `backing_off` for half an hour without a
request going out.

The second half is the page-identity anchor. `_looks_like_listing_page` knew
only idealista's `/inmueble/<id>/`, and fell through to "any 200 is the
listing" for anything else -- so a fotocasa URL redirected to a search page
would have been recorded as a live listing. That is the false confirmation of
#136, arriving at a second host.
"""

from types import SimpleNamespace

import pytest

from services.listing_status_service import HostBreakers, ListingStatusService

IDEALISTA = "https://www.idealista.com/en/inmueble/91523456/"
FOTOCASA = "https://www.fotocasa.es/en/buy/land/aviles/llaranes/190280914/d"


@pytest.fixture
def breakers():
    return HostBreakers(threshold=3, cooldown_s=1800)


class TestPerHost:
    def test_one_host_refusing_does_not_close_another(self, breakers):
        idealista = breakers.for_url(IDEALISTA)
        for _ in range(3):
            idealista.record_refusal("blocked")

        assert idealista.should_skip() is True
        assert breakers.for_url(FOTOCASA).should_skip() is False

    def test_the_same_host_shares_one_breaker_across_urls(self, breakers):
        """Per-URL counting would need `threshold` refusals per listing."""
        first = breakers.for_url("https://www.fotocasa.es/en/buy/land/a/b/1/d")
        second = breakers.for_url("https://www.fotocasa.es/es/comprar/terreno/c/d/2/d")

        assert first is second

    def test_scheme_and_case_do_not_split_a_host(self, breakers):
        assert breakers.for_url("http://WWW.Fotocasa.ES/x") is breakers.for_url(
            "https://www.fotocasa.es/y"
        )

    def test_state_names_every_host_it_has_dialled(self, breakers):
        breakers.for_url(IDEALISTA).record_refusal("blocked")
        breakers.for_url(FOTOCASA).record_success()

        state = breakers.state()

        assert set(state) == {"www.idealista.com", "www.fotocasa.es"}
        assert state["www.idealista.com"]["last_reason"] == "blocked"

    def test_reset_drops_every_host(self, breakers):
        """conftest calls this between tests; a partial reset leaks state."""
        for _ in range(3):
            breakers.for_url(IDEALISTA).record_refusal("blocked")
        for _ in range(3):
            breakers.for_url(FOTOCASA).record_refusal("timeout")

        breakers.reset()

        assert breakers.for_url(IDEALISTA).should_skip() is False
        assert breakers.for_url(FOTOCASA).should_skip() is False


class TestServiceUsesThem:
    def test_an_idealista_wall_leaves_fotocasa_reachable(self, monkeypatch):
        """End to end through `observe`: the wall must not spread."""
        service = ListingStatusService()

        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "idealista" in url:
                return SimpleNamespace(status_code=403, text="blocked", url=url)
            return SimpleNamespace(status_code=200, text="a listing", url=url)

        monkeypatch.setattr(
            "services.listing_status_service.request_with_retries",
            lambda fn, url, **kwargs: fake_get(url, **kwargs),
        )

        for _ in range(4):
            service.observe(IDEALISTA)

        # The fourth idealista call never left: the breaker answered it.
        assert len(calls) == 3

        observation = service.observe(FOTOCASA)

        assert observation.status == "active"
        assert observation.refusal is None
        assert FOTOCASA in calls


class TestPageIdentity:
    def test_a_fotocasa_redirect_to_a_search_page_is_not_a_live_listing(self):
        service = ListingStatusService()
        served = SimpleNamespace(
            url="https://www.fotocasa.es/en/buy/lands/asturias-province/all-zones/l"
        )

        assert service._looks_like_listing_page(FOTOCASA, served) is False

    def test_a_fotocasa_listing_served_as_itself_is(self):
        service = ListingStatusService()

        assert (
            service._looks_like_listing_page(FOTOCASA, SimpleNamespace(url=FOTOCASA))
            is True
        )

    def test_a_different_fotocasa_listing_is_not_this_one(self):
        service = ListingStatusService()
        other = "https://www.fotocasa.es/en/buy/land/aviles/llaranes/1902809/d"

        assert (
            service._looks_like_listing_page(FOTOCASA, SimpleNamespace(url=other))
            is False
        )

    def test_the_idealista_anchor_still_works(self):
        service = ListingStatusService()

        assert (
            service._looks_like_listing_page(IDEALISTA, SimpleNamespace(url=IDEALISTA))
            is True
        )
        assert (
            service._looks_like_listing_page(
                IDEALISTA, SimpleNamespace(url="https://www.idealista.com/")
            )
            is False
        )

    def test_a_url_with_no_recognisable_id_still_falls_back(self):
        """One row here is an agency's own site, whose grammar this cannot know."""
        service = ListingStatusService()
        agency = "https://inmobiliaria-example.es/detalle-inmuebles.php?id=2546"

        assert (
            service._looks_like_listing_page(agency, SimpleNamespace(url=agency))
            is True
        )
