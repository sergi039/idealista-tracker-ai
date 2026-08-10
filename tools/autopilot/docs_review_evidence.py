#!/usr/bin/env python3
"""Resolve the base-commit evidence a documentation-only review needs (issue #154).

`merge_bot.sh` hands `rx` a `base..head` diff, and the reviewer's contract is to
audit that diff and nothing else. For a PR that only writes down behaviour which
already shipped, the proof of every claim sits in the *base* commit — outside the
evidence the reviewer is allowed to read — so "unproven" is the only verdict it
can reach. That happened twice on #151: both reviews were right about the diff
and wrong about the repository, and the owner merged by hand. Adding `file:line`
citations to the documentation did not help, because a citation still points
outside the diff.

This closes the gap without widening what the reviewer may read. When every
changed path is documentation, the `path:line` citations in the *added*
documentation text are resolved against the base commit and printed here, which
makes them part of the review request itself. A docs PR that misdescribes the
code is then falsifiable from the prompt alone — which is the point of the gate —
while the absence of an implementation from the diff stops being an objection.

Nothing here decides a verdict; it only assembles evidence. The rules the
reviewer applies to it live next to the normal prompt in `merge_bot.sh`.

Usage:
    docs_review_evidence.py --repo DIR --base REV --head REV

Exit codes:
    0  documentation-only diff; the evidence block is on stdout
    3  the diff touches something other than documentation; nothing on stdout
    2  usage error, or git could not answer
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

# A documentation path carries no executable behaviour, so a diff confined to
# these can introduce no bug — only a false statement about the code. The list
# is deliberately narrow. `requirements.txt` is a `.txt` at the repository root
# and is very much executable configuration, so the non-Markdown extensions are
# granted only under `docs/`, and only to formats nothing installs or runs.
DOC_SUFFIXES_ANYWHERE = (".md",)
DOCS_DIR = "docs/"
# No `.svg`: it is a document format that can carry `<script>`, and a raster
# screenshot covers everything this repository's `docs/` actually needs.
DOC_SUFFIXES_UNDER_DOCS = (
    ".md",
    ".rst",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)

# How this repository's documentation actually cites code, measured on the PR
# that produced issue #154 (#151, `b01c3ac`): a bare path and a backticked
# symbol - "`utils/http.py` `HTTP_USER_AGENT`", "pinned by
# `tests/test_overpass_user_agent_and_refusal.py` - `TestOutgoingRequest`".
# Resolving only `path:line` would have found nothing on the very PR this exists
# to unblock, so both shapes are read.
#
# The lookbehind keeps a path match off URLs (`https://host/pkg/mod.py`) and off
# relative paths that would climb out of the repository.
# Longest extension first, and the trailing lookahead so an alternative cannot
# stop short inside a longer one: `js` would otherwise claim `credentials.json`
# and leave `on` behind, quietly citing a file that does not exist.
_CITATION_PATH = (
    r"(?:[A-Za-z0-9_.+-]+/)*[A-Za-z0-9_.+-]+"
    r"\.(?:bash|html|json|jsx|toml|yaml|tsx|pyi|sql|htm|css|cfg|ini|yml|py|sh|js|ts)"
    r"(?![A-Za-z0-9_])"
)
_CITATION_LINES = r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*"
PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9_./-])({_CITATION_PATH})(?::({_CITATION_LINES}))?"
)
# Markdown inline code is where this repository puts identifiers. A span is read
# as a symbol only if it is a bare identifier, so `services/http.py` and prose
# in backticks fall out on their own. Four characters minimum: shorter spans are
# almost always prose, and a symbol only ever gets used if it occurs in a file
# the same document cites, which filters the rest.
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}\Z")

# Content from these paths is never pasted into a review prompt, whatever a
# document claims to cite. The reviewer coordinator runs its own secret
# preflight over the prompt and would refuse the whole review — reported as
# UNAVAILABLE, retried every tick, with no visible reason — so refuse here,
# narrowly and out loud, instead.
#
# Matched against every path *component*, not just the basename: `prod.json`
# under `credentials/` is a credential however innocuous its own name looks.
#
# Each word must be delimited by something other than a letter, so `api_key.json`
# and `credentials/` are refused while `utils/tokenizer.py` and `monkey.py` are
# not - a needless refusal costs a reviewer the one excerpt that answers a claim.
#
# Two rules, because the words differ in how safely they can be matched loosely.
# `secret`, `token`, `credential` and `password` are never innocent inside a
# name, so only the *end* of the word is anchored: that catches
# `clientsecret.json` and `oauthtoken.json` while still excluding `tokenizer.py`
# and `secretary.md`, where the word runs on into something else.
SECRET_BEARING_WORD = re.compile(
    r"(?:credentials?|secrets?|tokens?|passwords?|passwd|passphrase|pwd"
    r"|auth|id_rsa|id_ed25519)(?:$|[^a-z])",
    re.IGNORECASE,
)
# `key` is different: `monkey.py` and `turkey.md` end in it and mean nothing.
#
# An allow-list of qualifiers was tried first and is the wrong shape - it fails
# open on every name nobody thought of, and three review rounds produced
# `apikey`, `clientsecret` and `clientkey` in turn. So `key` at the end of a
# name counts by default, and the few English words that happen to end in it are
# named instead. Unknown prefixes are treated as credentials, which is the
# direction a mistake here should fall.
INNOCENT_KEY_WORD = re.compile(
    r"(?:^|[^a-z])(?:mon|tur|don|hoc|joc|whis|lac|puc)keys?(?:$|[^a-z])",
    re.IGNORECASE,
)
SECRET_BEARING_KEY = re.compile(
    r"keys?(?:$|[^a-z])|(?:^|[^a-z])(?:keystore|keyfile)(?:$|[^a-z])",
    re.IGNORECASE,
)
SECRET_BEARING_SUFFIX = (".pem", ".p12", ".pfx", ".key", ".keystore", ".jks")


def is_secret_bearing(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith(SECRET_BEARING_SUFFIX):
        return True
    return any(
        component.startswith(".env")
        or SECRET_BEARING_WORD.search(component)
        # The exemption is removed from the name rather than applied to it:
        # skipping a whole component because it contains `monkey` would clear
        # `monkey_api_key.json` too.
        or SECRET_BEARING_KEY.search(INNOCENT_KEY_WORD.sub(" ", component))
        for component in lowered.split("/")
    )


# Documentation that an autonomous agent loads and acts on. It carries no
# executable behaviour in the machine's sense and every behavioural claim in it
# is still checkable against the base, so it stays eligible for this path - but
# the reviewer is told, because an added instruction is executed by the next
# agent run and that risk is visible in the diff itself.
#
# The dotted qualifier is not decoration: `CLAUDE.local.md` and
# `AGENTS.override.md` are loaded the same way the plain names are.
AGENT_INSTRUCTION_PATH = re.compile(
    r"(?:^|/)(?:claude|agents|gemini|copilot-instructions|skill)"
    r"(?:\.[a-z0-9_-]+)*\.md$"
    r"|^\.(?:claude|agents|codex|cursor|github/instructions)/"
    r"|(?:^|/)(?:skills|agents|prompts)/",
    re.IGNORECASE,
)


# A last guard on the content itself, for a credential that lives somewhere the
# path rule cannot predict. The reviewer coordinator refuses a whole prompt that
# matches its own patterns, which surfaces as UNAVAILABLE and is retried every
# tick with nothing said about why; dropping the one excerpt keeps the review
# runnable and states what was dropped.
CREDENTIAL_CONTENT = re.compile(
    r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[bap]-[A-Za-z0-9-]{10,}"
    # A connection URI carries its password in the userinfo, under a variable
    # name like DATABASE_URL that no key-name rule would ever flag.
    r"|[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s@/]+@"
    # A long quoted value assigned to a name that *contains* one of these words.
    # Matching the bare word missed both real shapes: `'client_secret': '...'`
    # quotes the key, and `SECRET_KEY = "..."` buries it in a longer identifier.
    # The value is anything long between quotes, not a curated character class:
    # `SECRET_KEY = "AAAA!BBBB..."` slipped past `[A-Za-z0-9_./+=-]` on one
    # punctuation mark. Over-matching here costs one stated refusal; under-
    # matching pastes a credential into a prompt.
    r"|(?i:[a-z0-9_]*(?:secret|token|password|passwd|credential"
    r"|api[_-]?key|private[_-]?key)[a-z0-9_]*)"
    # Six characters, not twenty: `PASSWORD = "hunter2!"` is a credential and a
    # twenty-character floor waved it through. The cost of the lower bound is a
    # stated refusal on a line like `PASSWORD_ENV = "DB_PASSWORD"`, which is a
    # far better trade than pasting a short password into an external prompt.
    r"[\"']?\s*[:=]\s*(\"[^\"\n]{6,}\"|'[^'\n]{6,}')"
)

# Lines of base context shown on each side of a cited line: enough to carry a
# function signature and the behaviour under it, not enough to bury the claim.
CONTEXT_LINES = 10
# The coordinator caps a review prompt at 128 KB including the diff evidence.
# A documentation diff is small, so this budget is generous; it exists so a
# pathological citation list cannot push the prompt over the limit and turn
# every tick into an UNAVAILABLE retry.
MAX_EVIDENCE_BYTES = 32_000
MAX_CITATIONS = 24
# Per cited file, so one popular identifier cannot crowd out the other files a
# document cites.
MAX_WINDOWS_PER_PATH = 4
# Resolution is (cited files x cited identifiers x file length), and this runs
# inside the merge bot's lock. A realistic docs PR is far below both caps;
# they exist so a pathological one cannot hold the lock while it grinds.
MAX_CITED_PATHS = 12
MAX_SYMBOLS = 24
# The changed-path list is orientation, not evidence; the diff itself carries
# the authoritative list. Bounded so a very wide docs PR cannot spend the whole
# prompt naming files.
MAX_LISTED_PATHS = 50
# A cited source file larger than this is not source anyone reads; refuse to
# load it rather than pull a generated blob into memory and into the prompt.
MAX_BLOB_BYTES = 2_000_000


# merge_bot.sh accepts this block only when its first line is this word followed
# by the base SHA that merge_bot itself resolved. An exit status alone is too
# weak a contract: a helper truncated to `raise SystemExit(0)` would exit clean
# with no output, and every PR - including one changing app.py - would then get
# the relaxed prompt from a classification that never ran.
EVIDENCE_SENTINEL = "DOCS-ONLY-EVIDENCE"


class GitError(RuntimeError):
    """git refused to answer a question this script cannot proceed without."""


@dataclass(frozen=True)
class Citation:
    """One excerpt to resolve out of the base commit.

    `line` is set when the documentation named one. `symbol` is set when the
    line was found by looking up a backticked identifier the documentation also
    used; it is carried so the excerpt can say which claim it answers. Both
    empty means the document only named the file, and the excerpt confirms
    whether that file exists.
    """

    path: str
    line: int | None = None
    symbol: str | None = None

    def label(self) -> str:
        if self.line is not None and self.symbol is None:
            return f"{self.path}:{self.line}"
        if self.symbol is not None:
            return f"{self.path} `{self.symbol}`"
        return self.path


def _git(repo: str, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {' '.join(args)}: {detail or result.returncode}")
    return result.stdout


def _git_text(repo: str, *args: str) -> str:
    return _git(repo, *args).decode("utf-8", errors="replace")


def is_documentation(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith(DOC_SUFFIXES_ANYWHERE):
        return True
    return lowered.startswith(DOCS_DIR) and lowered.endswith(DOC_SUFFIXES_UNDER_DOCS)


# A regular, non-executable file. Anything else in a diff is not documentation
# however it is named: 100755 is a script, 120000 a symlink whose target the
# diff does not show, 160000 a submodule pointer that moves whole trees of code.
# 000000 is the absent side of an addition or a deletion.
DOCUMENTATION_MODES = frozenset({"000000", "100644"})


@dataclass(frozen=True)
class ChangedPath:
    path: str
    old_mode: str
    new_mode: str

    def is_plain_file(self) -> bool:
        return {self.old_mode, self.new_mode} <= DOCUMENTATION_MODES


def changed_paths(repo: str, base: str, head: str) -> list[ChangedPath]:
    """Every path in `base..head`, with the file modes on both sides.

    `--raw` rather than `--name-only` because the name is not enough to decide
    this: an executable `tools/deploy.MD`, a `docs/notes.md` symlink, or a
    submodule pointer named `docs/vendor.md` all pass a suffix test and none of
    them is documentation.

    `--no-renames` so a renamed file reports both its old and its new path:
    rename detection would show only the destination, and a rename *out of*
    `docs/` into `services/` would then read as documentation-only.
    """
    raw = _git(repo, "diff", "--raw", "--no-renames", "-z", f"{base}..{head}")
    fields = raw.decode("utf-8", errors="replace").split("\0")

    changed: list[ChangedPath] = []
    index = 0
    while index < len(fields):
        meta = fields[index]
        index += 1
        if not meta.startswith(":"):
            continue
        # ":<old mode> <new mode> <old sha> <new sha> <status>"
        parts = meta[1:].split(" ")
        if len(parts) < 5 or index >= len(fields):
            raise GitError(f"unparsable `git diff --raw` record: {meta!r}")
        path = fields[index]
        index += 1
        if path:
            changed.append(ChangedPath(path=path, old_mode=parts[0], new_mode=parts[1]))
    return changed


def added_documentation_text(repo: str, base: str, head: str) -> str:
    """The lines this diff adds, without the surrounding context lines.

    Citations are collected from added text only. Context lines would re-collect
    citations that were already in the base and are not part of what this PR
    claims, and removed lines describe behaviour the PR is retracting.
    """
    diff = _git_text(repo, "diff", "--unified=0", f"{base}..{head}")
    added = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    return "\n".join(added)


def extract_paths(text: str) -> tuple[list[str], list[Citation]]:
    """Cited source paths, and the explicit `path:line` citations among them.

    Both lists keep document order and are de-duplicated, so the evidence reads
    in the order the documentation makes its claims.
    """
    paths: list[str] = []
    explicit: list[Citation] = []
    seen_paths: set[str] = set()
    seen_lines: set[Citation] = set()

    for match in PATH_RE.finditer(text):
        path, line_spec = match.group(1), match.group(2)
        if any(part == ".." for part in path.split("/")):
            continue
        if path not in seen_paths:
            seen_paths.add(path)
            paths.append(path)
        for line in _expand_line_spec(line_spec or ""):
            citation = Citation(path=path, line=line)
            if citation not in seen_lines:
                seen_lines.add(citation)
                explicit.append(citation)
    return paths, explicit


def extract_symbols(text: str) -> list[str]:
    """Backticked bare identifiers, in document order, de-duplicated."""
    symbols: list[str] = []
    seen: set[str] = set()
    for match in BACKTICK_RE.finditer(text):
        span = match.group(1).strip()
        if SYMBOL_RE.fullmatch(span) and span not in seen:
            seen.add(span)
            symbols.append(span)
    return symbols


def _definition_line(lines: list[str], symbol: str) -> int | None:
    """Where *symbol* is defined in *lines*, else where it first appears.

    A definition is what a claim about behaviour is anchored to; a bare mention
    is the fallback so a constant referenced from another module still resolves.
    """
    quoted = re.escape(symbol)
    definition = re.compile(
        rf"^\s*(?:async\s+def|def|class)\s+{quoted}\b"
        rf"|^\s*(?:export\s+)?(?:const|let|var|function)\s+{quoted}\b"
        rf"|^\s*{quoted}\s*(?:=[^=]|\(\)\s*\{{)"
    )
    mention = re.compile(rf"\b{quoted}\b")

    fallback: int | None = None
    for number, line in enumerate(lines, start=1):
        if definition.search(line):
            return number
        if fallback is None and mention.search(line):
            fallback = number
    return fallback


def plan_citations(repo: str, base: str, text: str) -> tuple[list[Citation], list[str]]:
    """What to quote out of the base, in the order the documentation cites it.

    Explicit `path:line` first, because the document asked for exactly that.
    Then, for each cited file, the definition of every backticked identifier the
    same document uses - which is how this repository actually writes citations.
    A file that yields neither still gets an entry, so that a path the base does
    not have is reported rather than silently dropped.

    Returns the plan and any notes about work the caps cut short, so the caps
    can be declared rather than read as "everything was checked".
    """
    all_paths, explicit = extract_paths(text)
    all_symbols = extract_symbols(text)
    paths = all_paths[:MAX_CITED_PATHS]
    symbols = all_symbols[:MAX_SYMBOLS]

    notes: list[str] = []
    if len(all_paths) > MAX_CITED_PATHS:
        notes.append(
            f"{len(all_paths) - MAX_CITED_PATHS} further cited file(s) were not "
            f"examined; this block resolves at most {MAX_CITED_PATHS}."
        )
    if len(all_symbols) > MAX_SYMBOLS:
        notes.append(
            f"{len(all_symbols) - MAX_SYMBOLS} further backticked identifier(s) "
            f"were not looked up; this block resolves at most {MAX_SYMBOLS}."
        )

    planned: list[Citation] = list(explicit)
    claimed: set[tuple[str, int]] = {
        (c.path, c.line) for c in explicit if c.line is not None
    }

    for path in paths:
        # Never even read one of these. `render_excerpt` refuses to quote them,
        # but the cheapest guarantee that a credential does not reach the prompt
        # is not to load it in the first place.
        if is_secret_bearing(path):
            if not any(c.path == path for c in planned):
                planned.append(Citation(path=path))
            continue

        lines = _blob_lines(repo, base, path)
        if isinstance(lines, str):
            # Unreadable or absent: `render_excerpt` re-resolves and reports why.
            if not any(c.path == path for c in planned):
                planned.append(Citation(path=path))
            continue

        found: list[Citation] = []
        skipped = 0
        for symbol in symbols:
            line = _definition_line(lines, symbol)
            if line is None or (path, line) in claimed:
                continue
            if len(found) >= MAX_WINDOWS_PER_PATH:
                skipped += 1
                continue
            claimed.add((path, line))
            found.append(Citation(path=path, line=line, symbol=symbol))
        if skipped:
            notes.append(
                f"{skipped} identifier(s) cited alongside {path} were not quoted; "
                f"this block shows at most {MAX_WINDOWS_PER_PATH} windows per file."
            )

        if found:
            planned.extend(sorted(found, key=lambda c: c.line or 0))
        elif not any(c.path == path for c in planned):
            planned.append(Citation(path=path))

    return planned, notes


def _expand_line_spec(spec: str) -> list[int]:
    """`261,298` -> [261, 298]; `120-140` -> [120] (the range's first line).

    A range is anchored on its start rather than expanded: the window below
    already shows a span, and expanding `1-400` would resolve four hundred
    overlapping excerpts out of one citation.
    """
    lines: list[int] = []
    for part in spec.split(","):
        start = part.split("-", 1)[0]
        if start.isdigit() and int(start) > 0:
            lines.append(int(start))
    return lines


def _blob_lines(repo: str, base: str, path: str) -> list[str] | str:
    """Base-commit text of *path* split into lines, or a reason it is unusable."""
    spec = f"{base}:{path}"
    exists = subprocess.run(
        ["git", "-C", repo, "cat-file", "-e", spec],
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        return "no such path in the base commit"

    size = int(_git_text(repo, "cat-file", "-s", spec).strip() or 0)
    if size > MAX_BLOB_BYTES:
        return f"file is {size} bytes in the base commit; too large to quote"

    blob = _git(repo, "cat-file", "blob", spec)
    try:
        return blob.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return "file is not UTF-8 text in the base commit"


def render_excerpt(repo: str, base: str, base_short: str, citation: Citation) -> str:
    # Two different failures, kept apart on purpose. An explicit `path:line` the
    # base cannot answer is UNRESOLVED: the documentation is already wrong.
    # A bare path the base does not have is NOT IN BASE, which is a fact the
    # reviewer weighs against what the surrounding text claims - documentation
    # legitimately names files that are generated, ignored, or yet to exist.
    unresolvable = "UNRESOLVED" if citation.line is not None else "NOT IN BASE"

    # REFUSED, not UNRESOLVED. The citation may be perfectly accurate; this bot
    # simply will not paste the file. Conflating the two would turn every
    # mention of a credential path into a BLOCKER about the documentation.
    if is_secret_bearing(citation.path):
        return (
            f"--- REFUSED: {citation.label()} ---\n"
            "  the path may carry credentials and is never quoted\n"
        )

    lines = _blob_lines(repo, base, citation.path)
    if isinstance(lines, str):
        return f"--- {unresolvable}: {citation.label()} ---\n  {lines}\n"

    if citation.line is None:
        return (
            f"--- {citation.path} (base {base_short}) ---\n"
            f"  present in the base commit, {len(lines)} lines. No identifier this\n"
            "  diff backticks occurs in it, so there is no narrower excerpt to show.\n"
        )

    if citation.line > len(lines):
        return (
            f"--- UNRESOLVED: {citation.label()} ---\n"
            f"  the base commit's copy of this file has only {len(lines)} lines\n"
        )

    first = max(1, citation.line - CONTEXT_LINES)
    last = min(len(lines), citation.line + CONTEXT_LINES)
    body = "".join(
        f"{'>' if number == citation.line else ' '} {number:>6} | {lines[number - 1]}\n"
        for number in range(first, last + 1)
    )
    if CREDENTIAL_CONTENT.search(body):
        return (
            f"--- REFUSED: {citation.label()} ---\n"
            "  this excerpt matches a credential pattern and is not quoted\n"
        )
    return (
        f"--- {citation.label()} "
        f"(base {base_short}, showing lines {first}-{last}) ---\n{body}"
    )


OPAQUE_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _opaque_content_warning(changed: list[ChangedPath]) -> list[str]:
    """Say plainly that nobody can see what an image contains.

    Git shows a binary marker and this block shows nothing, so a screenshot
    carrying a production password reaches main unread. That was already true
    of the strict prompt - no reviewer reads pixels - but a request that stays
    silent about it invites a PASS that means less than it looks like it means.

    Only what the merge would *add*. A deletion puts no unread pixel into main,
    and flagging it would send every screenshot removal to a human for nothing.
    """
    return _flagged_paths_notice(
        [
            entry.path
            for entry in changed
            if entry.new_mode != "000000" and entry.path.lower().endswith(OPAQUE_SUFFIX)
        ],
        [
            "UNREADABLE CONTENT: this diff changes image files whose contents appear",
            "neither in the diff nor below:",
        ],
        [
            "Nothing in this request can tell you what they show. If the surrounding",
            "documentation implies one carries a screenshot of a live system, say so;",
            "a person has to look.",
        ],
    )


def _agent_instruction_warning(paths: list[str]) -> list[str]:
    return _flagged_paths_notice(
        [path for path in paths if AGENT_INSTRUCTION_PATH.search(path)],
        [
            "AGENT INSTRUCTIONS: this diff changes files an autonomous agent loads and",
            "acts on:",
        ],
        [
            "Their added text is executed by the next agent run in every sense that",
            "matters, and unlike a behavioural claim it is fully visible in the diff.",
        ],
    )


def _flagged_paths_notice(
    flagged: list[str], lead: list[str], trail: list[str]
) -> list[str]:
    """A notice about *flagged*, saying so when the list itself was cut short.

    The 51st flagged path is the one a hostile change would use. A list that
    stops at fifty without saying so reads as the complete set.
    """
    if not flagged:
        return []
    overflow = len(flagged) - MAX_LISTED_PATHS
    return [
        *lead,
        *(f"  - {path}" for path in flagged[:MAX_LISTED_PATHS]),
        *(
            [
                f"  - TRUNCATED: {overflow} more, not listed. Read the diff for the",
                "    ones this notice could not name.",
            ]
            if overflow > 0
            else []
        ),
        *trail,
        "",
    ]


def build_evidence(repo: str, base: str, head: str, changed: list[ChangedPath]) -> str:
    """The evidence block for a documentation-only `base..head`."""
    paths = [entry.path for entry in changed]
    base_sha = _git_text(repo, "rev-parse", base).strip()
    base_short = base_sha[:7]

    citations, notes = plan_citations(
        repo, base, added_documentation_text(repo, base, head)
    )
    dropped = citations[MAX_CITATIONS:]
    kept = citations[:MAX_CITATIONS]

    sections: list[str] = []
    used = 0
    for citation in kept:
        excerpt = render_excerpt(repo, base, base_short, citation)
        if used + len(excerpt.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            dropped = citations[citations.index(citation) :]
            break
        sections.append(excerpt)
        used += len(excerpt.encode("utf-8"))

    header = [
        f"Documentation-only diff against base {base_sha}.",
        "",
        "Every path this diff touches is documentation:",
        *(f"  - {path}" for path in paths[:MAX_LISTED_PATHS]),
        *(
            [f"  - ... and {len(paths) - MAX_LISTED_PATHS} more"]
            if len(paths) > MAX_LISTED_PATHS
            else []
        ),
        "",
        *_opaque_content_warning(changed),
        *_agent_instruction_warning(paths),
        "Below is the base-commit source behind the files and identifiers this",
        "documentation cites. merge_bot.sh extracted it from the base and embedded",
        "it here, so it is part of this review request — not repository state you",
        "are being asked to go and read.",
        "",
    ]

    if not citations:
        header.append(
            "The added documentation cites no source file. Nothing to resolve."
        )
        header.append("")

    footer: list[str] = []
    if dropped:
        footer = [
            "",
            f"TRUNCATED: {len(dropped)} further citation(s) were not resolved, because",
            f"this block is capped at {MAX_CITATIONS} citations and "
            f"{MAX_EVIDENCE_BYTES} bytes. Unresolved:",
            *(f"  - {c.label()}" for c in dropped[:MAX_LISTED_PATHS]),
        ]
        if len(dropped) > MAX_LISTED_PATHS:
            footer.append(f"  - ... and {len(dropped) - MAX_LISTED_PATHS} more")
    if notes:
        footer.extend(["", *(f"TRUNCATED: {note}" for note in notes)])

    return "\n".join(header) + "".join(sections) + "\n".join(footer) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="repository to read (default: cwd)")
    parser.add_argument("--base", required=True, help="base revision")
    parser.add_argument("--head", required=True, help="head revision")
    args = parser.parse_args(argv)

    try:
        changed = changed_paths(args.repo, args.base, args.head)
        if not changed:
            print("no changed paths in this range", file=sys.stderr)
            return 3

        for entry in changed:
            if not is_documentation(entry.path):
                print(f"not documentation-only: {entry.path}", file=sys.stderr)
                return 3
            if not entry.is_plain_file():
                print(
                    f"not documentation-only: {entry.path} is mode "
                    f"{entry.old_mode}->{entry.new_mode}, not a plain file",
                    file=sys.stderr,
                )
                return 3

        sys.stdout.write(
            f"{EVIDENCE_SENTINEL} {_git_text(args.repo, 'rev-parse', args.base).strip()}\n"
        )
        sys.stdout.write(build_evidence(args.repo, args.base, args.head, changed))
    except GitError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
