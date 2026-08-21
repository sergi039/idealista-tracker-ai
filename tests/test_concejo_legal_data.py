"""The /construccion data contract (design review PASS WITH FIXES, 2026-08-21).

The guard is this test, not the import CLI: an agent edits JSON directly and
never passes through a CLI. Everything here is hermetic — tracked files and
pure functions, no DB, no network.
"""

import json
import re
from datetime import date, timedelta
from pathlib import Path

from services import concejo_legal
from services.buildability_catalog import (
    TOPICS,
    local_topics,
    mandatory_topics,
)

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "reference" / "legal" / "asturias_concejos.json"
SCOPE = ROOT / "reference" / "legal" / "scope.json"
CONCEJOS = ROOT / "reference" / "legal" / "concejos"
CHAPTERS = ROOT / "reference" / "legal" / "asturias"


def _snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["concejos"]


def _valid_payload(code="33049", name="Piloña") -> dict:
    return {
        "ine_code": code,
        "display_name": name,
        "facts": [
            {
                "topic": "pgo_status",
                "state": "present",
                "value": "approved",
                "confidence": "verified",
                "source": "BOPA (fixture)",
                "source_url": "https://example.org/bopa",
                "checked_at": "2026-08-01",
            }
        ],
    }


class TestSnapshot:
    def test_matches_the_committed_ine_reference(self):
        ine = json.loads(
            (ROOT / "data" / "ine_municipal.json").read_text(encoding="utf-8")
        )["municipalities"]
        expected = {
            code: row["name"]
            for code, row in ine.items()
            if row.get("province") == "33"
        }
        assert _snapshot() == expected

    def test_is_exactly_asturias(self):
        codes = sorted(_snapshot())
        assert len(codes) == 78
        assert codes[0] == "33001" and codes[-1] == "33078"

    def test_missing_snapshot_refuses_not_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            concejo_legal, "SNAPSHOT_PATH", Path("/nonexistent/snap.json")
        )
        try:
            concejo_legal.load_snapshot()
        except concejo_legal.SnapshotUnavailable:
            return
        raise AssertionError("a missing snapshot must refuse, not fall back")


class TestValidateCode:
    def test_accepts_only_known_asturias_codes(self):
        snap = _snapshot()
        assert concejo_legal.validate_code("33049", snap) == "33049"
        for bad in ("34001", "15030", "33999", "3304", "abc", "", None):
            assert concejo_legal.validate_code(bad, snap) is None

    def test_membership_is_checked_against_the_snapshot_itself(self):
        # The 33001-33078 range covers every real Asturias code, so with the
        # complete committed snapshot the membership check is redundant -- it
        # exists for a PARTIAL snapshot, where an in-range code absent from
        # the file must refuse rather than caption facts with no name.
        assert concejo_legal.validate_code("33049", {"33016": "X"}) is None


class TestScope:
    def test_scope_codes_are_valid_and_unique(self):
        raw = json.loads(SCOPE.read_text(encoding="utf-8"))
        codes = raw["concejos"]
        snap = _snapshot()
        assert codes, "an empty perimeter makes the coverage line meaningless"
        assert len(codes) == len(set(codes))
        for code in codes:
            assert concejo_legal.validate_code(code, snap) == code
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", raw["updated"])


class TestCatalog:
    def test_mandatory_set_is_non_empty(self):
        # all([]) would announce a completeness nobody established.
        assert mandatory_topics()

    def test_local_topics_carry_statute_and_patterns(self):
        for key in local_topics():
            topic = TOPICS[key]
            assert topic.regional_statute and topic.regional_statute[1], key
            assert topic.forbidden_patterns, key

    def test_chapter_anchors_exist(self):
        for key, topic in TOPICS.items():
            file_id, anchor = topic.chapter_anchor
            body = (CHAPTERS / f"{file_id}.html").read_text(encoding="utf-8")
            assert f'id="{anchor}"' in body, (key, file_id, anchor)


class TestChapterLint:
    def test_no_local_value_leaks_into_prose(self):
        bodies = {
            p.name: p.read_text(encoding="utf-8")
            for p in sorted(CHAPTERS.glob("*.html"))
        }
        assert bodies, "the regional chapters are missing"
        for key in local_topics():
            for pattern in TOPICS[key].forbidden_patterns:
                for name, body in bodies.items():
                    hit = re.search(pattern, body, re.IGNORECASE)
                    assert hit is None, (
                        f"{key}: value leaked into {name}: {hit.group(0)!r}"
                    )

    def test_the_lint_can_actually_fire(self):
        # A lint that matches nothing anywhere is indistinguishable from a
        # dead one; each pattern must catch its own canonical leak.
        leaks = {
            "pgo_status": "PGO aprobado en 2003",
            "cedula_regime": "en este concejo solo certificado",
            "coastal_pola": "es un concejo costero",
            "silence_period_months": "el plazo es de 3 meses",
            "first_occupation_regime": "licencia de primera ocupación obligatoria",
            "icio_rate_pct": "ICIO al 4%",
        }
        for key, sample in leaks.items():
            assert any(
                re.search(p, sample, re.IGNORECASE)
                for p in TOPICS[key].forbidden_patterns
            ), key


class TestTaggedUnion:
    def _problems(self, payload, code="33049"):
        return concejo_legal.validate_concejo_payload(code, payload, _snapshot())

    def test_a_valid_payload_passes(self):
        assert self._problems(_valid_payload()) == []

    def test_alias_names_are_rejected(self):
        # "Infiesto" resolves to 33049 through the portal alias table; the
        # canonical INE name is Piloña, and the validator must not accept the
        # alias (review round 5).
        assert self._problems(_valid_payload(name="Infiesto"))
        assert self._problems(
            _valid_payload(code="33039", name="San Esteban"), code="33039"
        )

    def test_present_requires_value_and_confidence(self):
        payload = _valid_payload()
        del payload["facts"][0]["value"]
        assert self._problems(payload)
        payload = _valid_payload()
        payload["facts"][0]["confidence"] = "hearsay"
        assert self._problems(payload)

    def test_verified_requires_https_source_url(self):
        payload = _valid_payload()
        del payload["facts"][0]["source_url"]
        assert self._problems(payload)
        payload = _valid_payload()
        payload["facts"][0]["source_url"] = "ftp://example.org"
        assert self._problems(payload)

    def test_not_confirmed_requires_searched_and_forbids_value(self):
        fact = {
            "topic": "icio_rate_pct",
            "state": "not_confirmed",
            "checked_at": "2026-08-01",
        }
        payload = {"ine_code": "33049", "display_name": "Piloña", "facts": [fact]}
        assert self._problems(payload)  # no searched
        fact["searched"] = "ordenanza fiscal (BOPA 2024)"
        assert self._problems(payload) == []
        fact["value"] = 3.5
        assert self._problems(payload)  # value on not_confirmed

    def test_state_is_an_explicit_discriminator(self):
        fact = {"topic": "pgo_status", "checked_at": "2026-08-01"}
        payload = {"ine_code": "33049", "display_name": "Piloña", "facts": [fact]}
        assert self._problems(payload)  # no state at all

    def test_future_dates_duplicates_and_domains_are_rejected(self):
        payload = _valid_payload()
        payload["facts"][0]["checked_at"] = (
            date.today() + timedelta(days=2)
        ).isoformat()
        assert self._problems(payload)
        payload = _valid_payload()
        payload["facts"].append(dict(payload["facts"][0]))
        assert self._problems(payload)  # duplicate topic
        payload = _valid_payload()
        payload["facts"][0]["value"] = "somewhat_approved"
        assert self._problems(payload)  # outside enum domain


class TestResearchedPredicate:
    def test_an_empty_file_is_not_research(self):
        assert not concejo_legal.researched(
            {"ine_code": "33049", "display_name": "Piloña", "facts": []}
        )
        assert not concejo_legal.researched(None)

    def test_not_confirmed_counts_as_search_performed(self):
        payload = {
            "facts": [
                {
                    "topic": "icio_rate_pct",
                    "state": "not_confirmed",
                    "searched": "x",
                    "checked_at": "2026-08-01",
                }
            ]
        }
        assert concejo_legal.researched(payload)


class TestTrackedConcejoFiles:
    def test_every_tracked_file_is_valid(self):
        snap = _snapshot()
        for path in sorted(CONCEJOS.glob("*.json")) if CONCEJOS.is_dir() else []:
            payload = json.loads(path.read_text(encoding="utf-8"))
            problems = concejo_legal.validate_concejo_payload(path.stem, payload, snap)
            assert problems == [], (path.name, problems)


class TestImageInclusion:
    """reference/legal/ must ride into the image through `COPY . .`.

    .dockerignore patterns without a slash match the context root only — the
    file says so itself at its `*.html` block — so `*.md`/`*.json`/`*.html`
    do not exclude nested reference files. This test implements exactly that
    documented semantic and fails if someone adds a pattern that would.
    """

    NEEDED = [
        "reference/legal/asturias_concejos.json",
        "reference/legal/scope.json",
        "reference/legal/asturias/01-permisos.html",
        "reference/legal/concejos",
        "reference/legal/asturias_full.html",
    ]

    def test_dockerignore_does_not_exclude_reference_legal(self):
        lines = [
            line.strip()
            for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for needed in self.NEEDED:
            for pattern in lines:
                if pattern.startswith("!"):
                    continue
                assert not self._matches(pattern, needed), (pattern, needed)

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        from fnmatch import fnmatch

        clean = pattern.rstrip("/")
        if "/" not in clean and "**" not in clean:
            # root-only: matches the first path segment alone
            return fnmatch(path.split("/")[0], clean)
        return fnmatch(path, clean) or path.startswith(clean + "/")
