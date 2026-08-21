"""The /construccion reference: regional dossier plus per-concejo overlay.

The one rule everything here serves is #98: an absence of research must never
render as a measured fact.  Seven review rounds (2026-08-21) shaped the
mechanism; the load-bearing decisions, each the answer to a FAIL:

* **The concejo is chosen by a person, never derived from a listing.**  The
  listing's own municipality string cannot prove a province (v2), and the
  geocoder's `agreed` self-confirms — `_row_province` reads the province off
  the very string it is checking (v3).  So this module resolves nothing; it
  validates an explicit choice.
* **One immutable snapshot of identity, inside the image.**
  `reference/legal/asturias_concejos.json` (78 rows, 33001…33078) is the only
  runtime source of codes and names.  `data/ine_municipal.json` is bind-mounted
  and its name index is cached, so a mutated copy could caption facts of
  concejo A with the name of concejo B (v5/v6).  A missing snapshot refuses
  the page — a check that could not run must not read as one that passed.
* **A fact file is a strict tagged union.**  `state: present` carries value +
  confidence (verified needs source and an https source_url);
  `state: not_confirmed` carries `searched`; `not_researched` is the absence
  of a record and is never written.  `researched()` — at least one record —
  is the single predicate the selector group and the coverage line share (v4).
* **No fallback.**  A local topic without a `present` record shows no value,
  of any type.  The regional statute is a different statement about a
  different subject and renders as its own row from the catalog's
  `regional_statute`, never as the concejo's value (v1, re-litigated and
  settled in v6: the two-row slot is not the v1 fallback).
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from services.buildability_catalog import TOPICS

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference" / "legal"
SNAPSHOT_PATH = REFERENCE_DIR / "asturias_concejos.json"
SCOPE_PATH = REFERENCE_DIR / "scope.json"
CONCEJOS_DIR = REFERENCE_DIR / "concejos"
CHAPTERS_DIR = REFERENCE_DIR / "asturias"
FULL_DOSSIER_PATH = REFERENCE_DIR / "asturias_full.html"

ASTURIAS_CODE_MIN, ASTURIAS_CODE_MAX = 33001, 33078

_CODE_RE = re.compile(r"^33\d{3}$")
_HTTPS_RE = re.compile(r"^https://", re.IGNORECASE)


class SnapshotUnavailable(RuntimeError):
    """The identity snapshot is missing or unreadable.

    The page refuses outright rather than falling back to the bind-mounted
    data/ine_municipal.json: identity served from a mutable file is the v5
    finding, and a fallback would reintroduce it exactly when the snapshot is
    broken."""


def load_snapshot() -> dict:
    try:
        raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        concejos = raw["concejos"]
    except (OSError, ValueError, KeyError) as exc:
        raise SnapshotUnavailable(str(exc)) from exc
    if not isinstance(concejos, dict) or not concejos:
        raise SnapshotUnavailable("snapshot holds no concejos")
    return concejos


def validate_code(value, snapshot: dict) -> Optional[str]:
    """The query-string code, or None. Range first, membership second: the
    range is closed (33001…33078 is the whole of Asturias) and does not
    depend on any file's content."""
    code = str(value or "").strip()
    if not _CODE_RE.match(code):
        return None
    if not ASTURIAS_CODE_MIN <= int(code) <= ASTURIAS_CODE_MAX:
        return None
    if code not in snapshot:
        return None
    return code


# --- the fact files ---------------------------------------------------------


def validate_concejo_payload(code: str, payload: dict, snapshot: dict) -> list:
    """Every violated rule, as strings; empty means valid.

    Shared verbatim by the CI test and the import CLI, because the CLI is a
    convenience and the test is the guard — an agent edits JSON directly and
    never passes through the CLI."""
    from utils.municipality_codes import build_index, match

    problems = []
    if payload.get("ine_code") != code:
        problems.append(f"ine_code {payload.get('ine_code')!r} != filename {code}")
    if code not in snapshot:
        problems.append(f"{code} is not an Asturias code in the snapshot")

    name = payload.get("display_name") or ""
    index = build_index({c: n for c, n in snapshot.items()})
    # Aliases OFF, explicitly: their default is True, and "Infiesto" passing
    # for 33049/Piloña is the v5 regression the review caught.
    if match(name, index, apply_aliases=False) != code:
        problems.append(f"display_name {name!r} is not the canonical INE name")

    facts = payload.get("facts")
    if not isinstance(facts, list):
        return problems + ["facts must be a list"]

    seen = set()
    for i, fact in enumerate(facts):
        where = f"facts[{i}]"
        topic = fact.get("topic")
        if topic not in TOPICS:
            problems.append(f"{where}: unknown topic {topic!r}")
            continue
        if topic in seen:
            problems.append(f"{where}: duplicate topic {topic!r}")
        seen.add(topic)

        state = fact.get("state")
        if state == "present":
            if "searched" in fact:
                problems.append(f"{where}: present must not carry 'searched'")
            if "value" not in fact:
                problems.append(f"{where}: present requires 'value'")
            else:
                problems.extend(
                    f"{where}: {p}" for p in _domain_problems(topic, fact["value"])
                )
            confidence = fact.get("confidence")
            if confidence not in ("verified", "agent_unverified"):
                problems.append(f"{where}: bad confidence {confidence!r}")
            if confidence == "verified":
                if not fact.get("source"):
                    problems.append(f"{where}: verified requires 'source'")
                if not _HTTPS_RE.match(str(fact.get("source_url") or "")):
                    problems.append(f"{where}: verified requires https source_url")
            elif confidence == "agent_unverified" and not fact.get("source"):
                problems.append(f"{where}: agent_unverified requires 'source'")
        elif state == "not_confirmed":
            for key in ("value", "confidence"):
                if key in fact:
                    problems.append(f"{where}: not_confirmed must not carry {key!r}")
            if not fact.get("searched"):
                problems.append(f"{where}: not_confirmed requires 'searched'")
        else:
            problems.append(
                f"{where}: state must be present|not_confirmed, got {state!r}"
            )

        checked = _parse_date(fact.get("checked_at"))
        if checked is None:
            problems.append(f"{where}: checked_at is not a valid date")
        elif checked > date.today():
            problems.append(f"{where}: checked_at is in the future")
    return problems


def _domain_problems(topic_key: str, value) -> list:
    domain = TOPICS[topic_key].value_domain
    if domain and all(isinstance(d, str) for d in domain):
        if value not in domain:
            return [f"value {value!r} not in {domain}"]
    elif len(domain) == 2:
        lo, hi = domain
        if not isinstance(value, (int, float)) or not lo <= value <= hi:
            return [f"value {value!r} outside [{lo}, {hi}]"]
    return []


def _parse_date(value) -> Optional[date]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def load_concejo(code: str) -> Optional[dict]:
    path = CONCEJOS_DIR / f"{code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def researched(payload: Optional[dict]) -> bool:
    """The one predicate the selector group and the coverage line share.
    A file's existence proves nothing: an empty `facts` is not research."""
    if not payload:
        return False
    return any(
        f.get("state") in ("present", "not_confirmed")
        for f in payload.get("facts") or []
    )


def is_stale(topic_key: str, checked_at) -> bool:
    checked = _parse_date(checked_at)
    if checked is None:
        return False
    return (date.today() - checked).days > TOPICS[topic_key].stale_after_days


@dataclass(frozen=True)
class Cell:
    """One topic of one concejo, for every surface that renders it."""

    state: str  # present | not_confirmed | not_researched
    value: object = None
    confidence: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    searched: Optional[str] = None
    checked_at: Optional[str] = None
    stale: bool = False


def cell_for(payload: Optional[dict], topic_key: str) -> Cell:
    for fact in (payload or {}).get("facts") or []:
        if fact.get("topic") != topic_key:
            continue
        if fact.get("state") == "present":
            return Cell(
                state="present",
                value=fact.get("value"),
                confidence=fact.get("confidence"),
                source=fact.get("source"),
                source_url=fact.get("source_url"),
                checked_at=fact.get("checked_at"),
                stale=is_stale(topic_key, fact.get("checked_at")),
            )
        if fact.get("state") == "not_confirmed":
            return Cell(
                state="not_confirmed",
                searched=fact.get("searched"),
                checked_at=fact.get("checked_at"),
            )
    return Cell(state="not_researched")


# --- scope and coverage -----------------------------------------------------


def load_scope() -> list:
    try:
        raw = json.loads(SCOPE_PATH.read_text(encoding="utf-8"))
        return list(raw.get("concejos") or [])
    except (OSError, ValueError):
        return []


def coverage(snapshot: dict) -> dict:
    """Counted with the same `researched()` the selector uses (v4's finding:
    two definitions of one word put an empty file in one and not the other).

    The wording the template renders is "search was performed", not
    "researched": every record can be `not_confirmed`, and counting those as
    knowledge would overstate what anyone established."""
    from services.buildability_catalog import mandatory_topics

    scope = [c for c in load_scope() if c in snapshot]
    researched_files = {}
    if CONCEJOS_DIR.is_dir():
        for path in sorted(CONCEJOS_DIR.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if researched(payload):
                researched_files[path.stem] = payload

    mandatory = mandatory_topics()
    searched_all_mandatory = confirmed = stale_count = 0
    for code in scope:
        payload = researched_files.get(code)
        if not payload:
            continue
        states = {t: cell_for(payload, t) for t in mandatory}
        if all(c.state != "not_researched" for c in states.values()):
            searched_all_mandatory += 1
        for topic_key in TOPICS:
            cell = cell_for(payload, topic_key)
            if cell.state == "present":
                confirmed += 1
                if cell.stale:
                    stale_count += 1

    return {
        "scope_total": len(scope),
        "searched_any": sum(1 for c in scope if c in researched_files),
        "searched_all_mandatory": searched_all_mandatory,
        "confirmed_values": confirmed,
        "stale_values": stale_count,
        "beyond_scope": sorted(set(researched_files) - set(scope)),
    }


def load_full_dossier():
    """The uncompressed dossier, or None. Rendered WITHOUT the concejo
    selector and without slots: it is a purely regional reading document, and
    the round-6 contract (a value in prose must not sit next to a selected
    concejo) is kept by there being no concejo context on that page at all.
    It also lives outside CHAPTERS_DIR on purpose -- the chapter lint forbids
    local-topic values in chapter prose, and this document legitimately
    carries the regional statutes in full."""
    try:
        return FULL_DOSSIER_PATH.read_text(encoding="utf-8")
    except OSError:
        return None


# --- the regional chapters --------------------------------------------------

_CHAPTER_HEAD_RE = re.compile(r'<h2\s+id="(?P<id>[a-z0-9-]+)"\s*>(?P<title>[^<]+)</h2>')


def load_chapters() -> list:
    """Chapters are HTML fragments, not Markdown: the repo carries no
    markdown renderer and adding a dependency for eight static files is not
    worth it. They are committed and reviewed like templates, which is what
    makes rendering them unescaped acceptable."""
    chapters = []
    if not CHAPTERS_DIR.is_dir():
        return chapters
    for path in sorted(CHAPTERS_DIR.glob("*.html")):
        body = path.read_text(encoding="utf-8")
        head = _CHAPTER_HEAD_RE.search(body)
        chapters.append(
            {
                "file_id": path.stem,
                "anchor": head.group("id") if head else path.stem,
                "title": head.group("title").strip() if head else path.stem,
                "body": body,
            }
        )
    return chapters
