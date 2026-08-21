"""Four claims this repository made that had stopped being true (#453 review).

Three of them were prose and one was a tuple, and the tuple is the one worth
starting with, because it shows what the other three cost.

`WEIGHTLESS_SCORE_KEYS` carried a comment promising that a criterion "cannot
be added to the scorer and forgotten by the gate" -- above a hand-written
`("pool_score", "hazard_score")`, which is precisely a thing that can be
forgotten. The promise and the mechanism disagreed, and only the promise was
load-bearing: `routes/main_routes.py` reads that tuple to decide which save
needs the dry-run preview before it re-scores a subscription.

The other three are sentences. `CLAUDE.md` said `enrich_osm_amenities` was
still #352's open gap eight hours after #460 closed it; the hazard card
labelled high severity "Emitting" one line above its own disclosure that
OpenStreetMap establishes nothing about emissions; and `DEFAULT_HAZARD`
presented two chosen thresholds as if the measurement behind them had picked
them.

None of these could fail a test, which is why they drifted.
"""

from pathlib import Path

import pytest

from tests import setup_test_environment

setup_test_environment()

from models import Property  # noqa: E402,F401
from services.property_scoring_service import (  # noqa: E402
    WEIGHTLESS_SCORE_KEYS,
    BasePropertyScorer,
    _weightless_score_keys,
)

ROOT = Path(__file__).resolve().parent.parent


def _scorers():
    import services.property_scoring_service as module

    return [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BasePropertyScorer)
        and value is not BasePropertyScorer
        and getattr(value, "DEFAULT_INVESTMENT_WEIGHTS", None)
    ]


class TestTheWeightlessSetIsDerived:
    """The gate's list is read off the scorers, so it cannot be forgotten."""

    def test_it_names_the_criteria_that_ship_at_zero(self):
        assert WEIGHTLESS_SCORE_KEYS == ("hazard_score", "pool_score")

    def test_every_scorer_really_ships_them_at_zero(self):
        """The derivation's own premise, asserted rather than assumed."""
        for scorer in _scorers():
            for key in WEIGHTLESS_SCORE_KEYS:
                assert float(scorer.DEFAULT_INVESTMENT_WEIGHTS.get(key, 0.0)) == 0.0
                assert float(scorer.DEFAULT_LIFESTYLE_WEIGHTS.get(key, 0.0)) == 0.0

    def test_a_new_weightless_criterion_is_picked_up_without_being_listed(
        self, monkeypatch
    ):
        """The whole point. Add one to the scorers and the gate sees it."""
        for scorer in _scorers():
            monkeypatch.setitem(scorer.DEFAULT_INVESTMENT_WEIGHTS, "noise_score", 0.0)
            monkeypatch.setitem(scorer.DEFAULT_LIFESTYLE_WEIGHTS, "noise_score", 0.0)
        assert "noise_score" in _weightless_score_keys()

    def test_a_criterion_weightless_in_one_category_only_is_not_one(self):
        """`sea_score` is 0.0 in both of the garage scorer's weight sets and
        non-zero elsewhere. That is a statement about garages, not a criterion
        shipped off, and demanding a preview for it would ask the owner to
        confirm a save that changes nothing."""
        garage = [s for s in _scorers() if s.category == "garage"]
        assert garage, "the garage scorer moved; this test is about it"
        assert float(garage[0].DEFAULT_INVESTMENT_WEIGHTS.get("sea_score", 1.0)) == 0.0
        assert "sea_score" not in WEIGHTLESS_SCORE_KEYS

    def test_the_gate_reads_this_and_not_a_copy(self):
        source = (ROOT / "routes" / "main_routes.py").read_text()
        assert "WEIGHTLESS_SCORE_KEYS" in source
        assert '"pool_score"' not in source or '"hazard_score"' not in source, (
            "the gate has grown its own copy of the list"
        )


class TestTheSentencesSayWhatIsTrue:
    """Prose that had gone stale. A test cannot read English, so each of these
    pins the specific words that were wrong, and says what replaced them."""

    def test_claude_md_no_longer_calls_352_open(self):
        claude = (ROOT / "CLAUDE.md").read_text()
        assert "#352's open gap" not in claude, (
            "#460 closed it; the file still describes the hole"
        )
        assert "Three of its four take the row under" not in claude
        # Line-wrapped in the file, so the assertion is on the unwrapped text
        # rather than on where the paragraph happens to break.
        unwrapped = " ".join(claude.split())
        assert "every one of them takes the row under `FOR UPDATE`" in unwrapped

    def test_the_amenity_writer_really_does_lock_now(self):
        """The sentence above is only worth pinning while the code backs it."""
        source = (ROOT / "services" / "enrichment_service.py").read_text()
        body = source[source.index("def enrich_osm_amenities") :][:4000]
        assert "check_writable(prop, commit)" in body
        assert "locked_write(prop, locked=locked, commit=commit)" in body

    def test_the_severity_badge_does_not_claim_an_emission(self):
        """OSM establishes that a cement works exists. The block says in its
        own disclosure that it establishes nothing about emissions, so the
        badge beside it must not."""
        i18n = (ROOT / "utils" / "i18n.py").read_text()
        assert '"hazard_severity_high": "Emitting"' not in i18n
        assert '"hazard_severity_high": "Heavy industry"' in i18n
        assert '"hazard_severity_high": "Emisora"' not in i18n
        assert '"hazard_severity_high": "Industria pesada"' in i18n
        # And the disclosure it used to contradict is still there.
        assert "It says nothing about its emissions" in i18n

    def test_the_hazard_thresholds_say_they_are_chosen(self):
        source = (ROOT / "services" / "property_scoring_service.py").read_text()
        block = source[source.index("DEFAULT_HAZARD") - 1800 :][:1900]
        assert "the thresholds are chosen" in block
        assert "1.12 km" in block, "the measurement behind them is still named"


@pytest.mark.parametrize("key", ["pool_score", "hazard_score"])
def test_the_gate_still_covers_both_shipped_criteria(key):
    """A regression net for the derivation itself: whatever else changes,
    these two must keep needing the preview."""
    assert key in WEIGHTLESS_SCORE_KEYS
