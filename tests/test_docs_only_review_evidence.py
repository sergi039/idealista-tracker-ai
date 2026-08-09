"""What a documentation-only review is allowed to see, and what it must refuse.

Issue #154: the merge gate could not pass a documentation-only PR, because the
reviewer audits the embedded diff and the code a docs PR describes lives in the
base commit. `tools/autopilot/docs_review_evidence.py` resolves the `path:line`
citations in the added documentation against the base so the claims become
checkable from the review request alone.

These run against a real git repository - real commits, real `git diff`, real
`git cat-file` - because every interesting case here (a rename out of `docs/`, a
citation to a line the base does not have, a `.py` file sitting under `docs/`)
is a question about what git actually reports, and a stubbed git would only
prove that the stub agrees with the test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "autopilot" / "docs_review_evidence.py"

DOCS_ONLY = 0
NOT_DOCS_ONLY = 3

# A recognisable base file to cite. The marker strings are what the assertions
# look for in the emitted excerpt, so they must not appear anywhere else.
SERVICE_SOURCE = "\n".join(
    [f"# filler line {n}" for n in range(1, 20)]
    + [
        "def refuse_remark_only_query(remark):",
        "    # MARKER_REFUSAL overpass-api.de refuses this outright",
        "    raise ValueError('remark-only queries are refused upstream')",
    ]
    + [f"# trailing filler {n}" for n in range(1, 10)]
)
MARKER_LINE = 21  # the MARKER_REFUSAL comment, 1-indexed
DEFINITION_LINE = 20  # `def refuse_remark_only_query`


@pytest.fixture(autouse=True)
def _needs_git():
    if shutil.which("git") is None:
        pytest.skip("docs_review_evidence.py needs git")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _write(repo: Path, relative: str, body: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _base_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository whose base commit holds the code documentation will cite."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "services/enrichment_service.py", SERVICE_SOURCE + "\n")
    _write(repo, "docs/STATE.md", "# State\n")
    _write(repo, "requirements.txt", "flask\n")
    base = _commit(repo, "base")
    return repo, base


def _run(repo: Path, base: str, head: str = "HEAD"):
    return subprocess.run(
        ["python3", str(SCRIPT), "--repo", str(repo), "--base", base, "--head", head],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cited_base_source_is_embedded_in_the_evidence(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nOverpass refuses remark-only queries\n"
        f"(`services/enrichment_service.py:{MARKER_LINE}`).\n",
    )
    _commit(repo, "docs: write down the refusal")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "MARKER_REFUSAL" in result.stdout, (
        "the cited base source was not embedded, so the reviewer still has "
        "nothing to check the documentation against"
    )
    assert f"> {MARKER_LINE:>6} |" in result.stdout, "the cited line is not marked"
    assert "Documentation-only diff against base" in result.stdout
    assert "docs/STATE.md" in result.stdout


def test_a_bare_path_and_a_backticked_symbol_resolve_to_the_definition(tmp_path):
    """How this repository actually cites code, measured on #151 (`b01c3ac`).

    Its documentation writes "`utils/http.py` `HTTP_USER_AGENT`", never
    `path:line`. Resolving only `path:line` would find nothing on the very PR
    that produced issue #154.
    """
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nThe refusal is `refuse_remark_only_query`\n"
        "(`services/enrichment_service.py`).\n",
    )
    _commit(repo, "docs: cite by path and symbol")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "services/enrichment_service.py `refuse_remark_only_query`" in result.stdout
    assert f"> {DEFINITION_LINE:>6} |" in result.stdout, (
        "not anchored on the definition"
    )
    assert "MARKER_REFUSAL" in result.stdout


def test_a_citation_the_base_cannot_resolve_is_reported_unresolved(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/STATE.md", "# State\n\nSee `services/gone.py:12`.\n")
    _commit(repo, "docs: cite a file that is not there")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "UNRESOLVED: services/gone.py:12" in result.stdout
    assert "no such path in the base commit" in result.stdout


def test_a_bare_path_the_base_lacks_is_reported_but_not_as_unresolved(tmp_path):
    """Kept apart from UNRESOLVED on purpose.

    A `path:line` the base cannot answer is documentation that is already
    wrong. A bare path may legitimately name something generated, ignored or
    still to be written (`docker-compose.override.yml`, a worktree `.env`), so
    the reviewer is handed the fact and decides from the surrounding text.
    """
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nA second checkout writes its own `docker-compose.override.yml`.\n",
    )
    _commit(repo, "docs: mention an ignored file")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "NOT IN BASE: docker-compose.override.yml" in result.stdout
    assert "UNRESOLVED" not in result.stdout


def test_a_json_path_is_not_truncated_to_a_js_one(tmp_path):
    """`js` must not claim `credentials.json` and leave `on` behind."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "static/app.json", '{"a": 1}\n')
    base = _commit(repo, "add a json file")

    _write(repo, "docs/STATE.md", "# State\n\nConfig lives in `static/app.json`.\n")
    _commit(repo, "docs: cite the json file")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "static/app.json" in result.stdout
    assert "NOT IN BASE" not in result.stdout, (
        "the extension alternation stopped short and cited a file that never existed"
    )


def test_a_line_beyond_the_end_of_the_file_is_reported_unresolved(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nSee `services/enrichment_service.py:9000`.\n",
    )
    _commit(repo, "docs: cite a line that does not exist")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "UNRESOLVED: services/enrichment_service.py:9000" in result.stdout
    assert "only 31 lines" in result.stdout


def test_a_diff_that_also_changes_code_is_not_documentation_only(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/STATE.md", "# State\n\nUpdated.\n")
    _write(repo, "services/enrichment_service.py", SERVICE_SOURCE + "\n# changed\n")
    _commit(repo, "docs plus code")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == "", "a code change must not get the relaxed prompt"
    assert "not documentation-only" in result.stderr


def test_requirements_txt_is_not_documentation(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "requirements.txt", "flask\nrequests\n")
    _commit(repo, "add a dependency")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY, (
        "a root .txt is installable configuration, not documentation"
    )
    assert result.stdout == ""


def test_an_executable_markdown_file_is_not_documentation(tmp_path):
    """A suffix does not make a file inert. `tools/deploy.MD` mode 100755 runs."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "tools/deploy.MD", "#!/bin/sh\necho hi\n")
    (repo / "tools" / "deploy.MD").chmod(0o755)
    _commit(repo, "add an executable .MD")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""
    assert "not a plain file" in result.stderr


def test_a_markdown_symlink_is_not_documentation(tmp_path):
    """The diff shows the link target as text; it does not show what it points at."""
    repo, base = _base_repo(tmp_path)
    (repo / "docs" / "shortcut.md").symlink_to("../services/enrichment_service.py")
    _commit(repo, "add a docs symlink")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""
    assert "not a plain file" in result.stderr


def test_a_submodule_pointer_named_like_a_doc_is_not_documentation(tmp_path):
    """Mode 160000 moves a whole tree of code that the diff never shows."""
    repo, base = _base_repo(tmp_path)
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q", "-b", "main")
    _write(inner, "code.py", "print('hi')\n")
    inner_head = _commit(inner, "inner")

    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{inner_head},docs/vendor.md",
    )
    _git(
        repo,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "add a gitlink named like a doc",
    )

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""
    assert "not a plain file" in result.stderr


def test_a_credential_directory_is_refused_even_with_an_innocent_filename(tmp_path):
    """The rule reads every path component, not only the basename."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "credentials/prod.json", '{"value": "%s"}\n' % CREDENTIAL_MARKER)
    _write(repo, "config/api_key.json", '{"value": "%s"}\n' % CREDENTIAL_MARKER)
    base = _commit(repo, "add credential files")

    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nSee `credentials/prod.json:1` and `config/api_key.json:1`.\n",
    )
    _commit(repo, "docs: cite them")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert CREDENTIAL_MARKER not in result.stdout
    assert result.stdout.count("REFUSED:") == 2


def test_a_credential_path_without_a_separator_is_refused(tmp_path):
    """`apikey.json` carries exactly what `api_key.json` does."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "config/apikey.json", '{"value": "%s"}\n' % CREDENTIAL_MARKER)
    base = _commit(repo, "add an apikey file")

    _write(repo, "docs/STATE.md", "# State\n\nSee `config/apikey.json:1`.\n")
    _commit(repo, "docs: cite it")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert CREDENTIAL_MARKER not in result.stdout
    assert "REFUSED: config/apikey.json:1" in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        "config/clientsecret.json",
        "config/oauthtoken.json",
        "config/apikey.json",
        # An allow-list of qualifiers failed open on each of these in turn, one
        # review round apiece. `key` at the end of a name now counts by default.
        "config/clientkey.json",
        "config/tenantkey.json",
        # The exemption for words ending in `key` must not clear a name that
        # also carries a real marker.
        "config/monkey_api_key.json",
        # Verified reachable in review: neither the word list nor the content
        # rule caught `{"username":"admin","pass":"hunter2"}` here.
        "config/basic_auth.json",
    ],
)
def test_a_credential_name_welded_to_its_qualifier_is_refused(tmp_path, path):
    """`clientsecret.json` carries what `client_secret.json` does."""
    repo, base = _base_repo(tmp_path)
    _write(repo, path, '{"value": "%s"}\n' % CREDENTIAL_MARKER)
    base = _commit(repo, "add a credential file")

    _write(repo, "docs/STATE.md", f"# State\n\nSee `{path}:1`.\n")
    _commit(repo, "docs: cite it")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert CREDENTIAL_MARKER not in result.stdout
    assert f"REFUSED: {path}:1" in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        # Loaded the same way the plain names are, so the qualifier must not
        # be a way past the notice.
        "CLAUDE.local.md",
        "AGENTS.override.md",
        ".claude/rules.md",
        # A skill is an instruction file too, and a hostile one can tell a
        # reviewer agent to return PASS without reading anything.
        ".agents/skills/reviewer/SKILL.md",
        "docs/skills/deploy.md",
    ],
)
def test_agent_instruction_files_are_flagged_for_the_reviewer(tmp_path, path):
    """CLAUDE.md is not executable, but the next agent run executes it."""
    repo, base = _base_repo(tmp_path)
    _write(repo, path, "# Rules\n\nAlways run the full suite.\n")
    _commit(repo, "docs: add a rule")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "AGENT INSTRUCTIONS:" in result.stdout
    assert f"  - {path}" in result.stdout


def test_deleting_a_guardrail_from_agent_instructions_is_still_flagged(tmp_path):
    """The dangerous edit to an instruction file is often a deletion.

    Nothing is added, so a rule written around added lines would let a PR strike
    a guardrail out and match no objection at all.
    """
    repo, base = _base_repo(tmp_path)
    _write(repo, "CLAUDE.md", "# Rules\n\nNever read or echo .env.\nRun the suite.\n")
    base = _commit(repo, "docs: add rules")

    _write(repo, "CLAUDE.md", "# Rules\n\nRun the suite.\n")
    _commit(repo, "docs: drop a rule")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "AGENT INSTRUCTIONS:" in result.stdout
    assert "  - CLAUDE.md" in result.stdout


def test_an_image_is_declared_unreadable_rather_than_passed_over(tmp_path):
    """Neither the diff nor this block shows what a screenshot contains."""
    repo, base = _base_repo(tmp_path)
    (repo / "docs" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    _commit(repo, "docs: add a screenshot")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "UNREADABLE CONTENT:" in result.stdout
    assert "  - docs/shot.png" in result.stdout


def test_deleting_an_image_is_not_declared_unreadable(tmp_path):
    """A deletion puts no unread pixel into main.

    Flagging it would send every screenshot removal to a human for nothing,
    because the prompt asks for a BLOCKER on the notice.
    """
    repo, base = _base_repo(tmp_path)
    (repo / "docs" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    base = _commit(repo, "docs: add a screenshot")

    (repo / "docs" / "shot.png").unlink()
    _commit(repo, "docs: drop the screenshot")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "UNREADABLE CONTENT:" not in result.stdout


def test_a_text_only_docs_change_is_not_declared_unreadable(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/STATE.md", "# State\n\nUpdated.\n")
    _commit(repo, "docs: update")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "UNREADABLE CONTENT:" not in result.stdout


def test_a_markdown_path_is_not_resolved_as_evidence(tmp_path):
    """One document is not evidence for another, and the README says so."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/STATE.md", "# State\n\nSee `docs/UNIVERSAL_PROPERTIES.md`.\n")
    _commit(repo, "docs: cross-reference")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "cites no source file" in result.stdout


def test_a_long_agent_instruction_list_says_it_was_cut_short(tmp_path):
    """The 51st flagged file is the one a hostile change would use."""
    repo, base = _base_repo(tmp_path)
    for n in range(51):
        _write(repo, f"prompts/rule_{n:02d}.md", f"# Rule {n}\n")
    _commit(repo, "docs: add many prompts")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "AGENT INSTRUCTIONS:" in result.stdout
    assert "TRUNCATED: 1 more, not listed" in result.stdout, (
        "a list that stops at fifty without saying so reads as the complete set"
    )


def test_an_ordinary_docs_change_is_not_flagged_as_agent_instructions(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/STATE.md", "# State\n\nUpdated.\n")
    _commit(repo, "docs: update")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "AGENT INSTRUCTIONS:" not in result.stdout


def test_an_svg_under_docs_is_not_documentation(tmp_path):
    """SVG is a document format that can carry a script element."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/diagram.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>\n")
    _commit(repo, "add a diagram")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""


def test_an_ordinary_word_containing_key_is_not_refused(tmp_path):
    """A needless refusal costs the reviewer the excerpt that answers the claim."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "utils/tokenizer.py", "def split(text):\n    return text.split()\n")
    _write(repo, "utils/monkey.py", "PATCHED = True\n")
    base = _commit(repo, "add ordinary modules")

    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nSee `utils/tokenizer.py:1` and `utils/monkey.py:1`.\n",
    )
    _commit(repo, "docs: cite them")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "REFUSED" not in result.stdout
    assert "def split" in result.stdout


@pytest.mark.parametrize(
    ("body", "leaked"),
    [
        # Quoted key, as JSON and Python dict literals write it.
        ("def load():\n    return {'client_secret': '" + "A" * 32 + "'}\n", "A" * 32),
        # The word buried in a longer identifier, which is how Flask spells it.
        ('SECRET_KEY = "' + "B" * 32 + '"\n', "B" * 32),
        # One punctuation mark used to be enough to fall out of the value class.
        ('SECRET_KEY = "' + "C" * 20 + "!" + "D" * 20 + '"\n', "C" * 20 + "!"),
        # A connection URI hides the password in the userinfo, under a name no
        # key-name rule would flag.
        (
            'DATABASE_URL = "postgresql://admin:hunter2-horse-staple@db/prod"\n',
            "hunter2-horse-staple",
        ),
        # A short password is still a password; a 20-character floor waved it
        # through.
        ('PASSWORD = "hunter2!"\n', "hunter2!"),
    ],
)
def test_a_credential_shaped_excerpt_is_dropped_rather_than_quoted(
    tmp_path, body, leaked
):
    """The path rule cannot predict every location; the content is checked too."""
    repo, base = _base_repo(tmp_path)
    _write(repo, "services/settings.py", body)
    base = _commit(repo, "add a settings module")

    _write(
        repo, "docs/STATE.md", "# State\n\nSettings live in `services/settings.py:1`.\n"
    )
    _commit(repo, "docs: cite settings")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert leaked not in result.stdout
    assert "matches a credential pattern" in result.stdout


def test_a_python_file_under_docs_is_not_documentation(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/generate_screenshots.py", "print('hi')\n")
    _commit(repo, "add a script under docs/")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""


def test_a_rename_out_of_docs_is_not_documentation_only(tmp_path):
    """Rename detection would show only the destination path.

    `docs/notes.md -> services/notes.py` is a code addition however git chooses
    to describe it, so the diff is read with `--no-renames` and both ends have
    to be documentation.
    """
    repo, base = _base_repo(tmp_path)
    _write(repo, "docs/notes.md", "notes\n")
    base = _commit(repo, "add notes")

    (repo / "docs" / "notes.md").rename(repo / "services" / "notes.py")
    _commit(repo, "move notes into services")

    result = _run(repo, base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""


def test_citations_only_in_removed_lines_are_not_resolved(tmp_path):
    """A citation the PR deletes is not a claim the PR is making."""
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "docs/STATE.md",
        f"# State\n\nOld claim about `services/enrichment_service.py:{MARKER_LINE}`.\n",
    )
    base = _commit(repo, "docs: old claim")

    _write(repo, "docs/STATE.md", "# State\n\nClaim withdrawn.\n")
    _commit(repo, "docs: withdraw it")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "MARKER_REFUSAL" not in result.stdout
    assert "cites no source file" in result.stdout


# The refusal these two exercise is keyed on the *path*, so the file bodies
# carry a plain marker rather than anything credential-shaped. That is not
# squeamishness: `rx` runs a secret preflight over the diff it is given, and a
# realistic-looking token in a fixture makes every review of this repository
# come back UNAVAILABLE while the file is in range. Observed while writing this
# twice - first with a fixture whose body imitated a Google refresh token, then
# with a comment here that quoted the offending literal to explain the first.
# Describe the shape; never write it down.
CREDENTIAL_MARKER = "CREDENTIAL_MUST_NOT_APPEAR"


def test_a_secret_bearing_path_is_refused_rather_than_quoted(tmp_path):
    """Quoting it would trip the coordinator's own secret preflight.

    That failure surfaces as UNAVAILABLE and is retried every tick with no
    visible reason, so the refusal is made here, narrowly and out loud.
    """
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "config/service_account_credentials.json",
        '{"value": "%s"}\n' % CREDENTIAL_MARKER,
    )
    base = _commit(repo, "add credentials")

    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nSee `config/service_account_credentials.json:1`.\n",
    )
    _commit(repo, "docs: cite the credential file")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert CREDENTIAL_MARKER not in result.stdout, (
        "content of a credential-bearing path reached the review prompt"
    )
    assert "REFUSED: config/service_account_credentials.json:1" in result.stdout
    assert "never quoted" in result.stdout
    assert "UNRESOLVED" not in result.stdout, (
        "a refusal is not a citation the base cannot answer; conflating them "
        "would turn every mention of a credential path into a BLOCKER"
    )


def test_a_secret_bearing_path_cited_bare_is_also_refused(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(repo, "config/oauth_token.json", '{"value": "%s"}\n' % CREDENTIAL_MARKER)
    base = _commit(repo, "add a token file")

    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nCredentials live in `config/oauth_token.json`.\n",
    )
    _commit(repo, "docs: cite the token file")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert CREDENTIAL_MARKER not in result.stdout
    assert "never quoted" in result.stdout


def test_too_many_citations_are_capped_and_the_cap_is_declared(tmp_path):
    """A silent cap would read as 'everything was checked'."""
    repo, base = _base_repo(tmp_path)
    citations = "\n".join(
        f"- see `services/enrichment_service.py:{line}`" for line in range(1, 32)
    )
    _write(repo, "docs/STATE.md", f"# State\n\n{citations}\n")
    _commit(repo, "docs: cite everything")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "TRUNCATED:" in result.stdout
    assert "services/enrichment_service.py:31" in result.stdout


def test_the_per_file_window_cap_is_declared(tmp_path):
    """A silent cap reads as 'everything in that file was checked'."""
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "services/many.py",
        "".join(f"def symbol_{n}():\n    return {n}\n" for n in range(1, 8)),
    )
    base = _commit(repo, "add a module with many symbols")

    symbols = " ".join(f"`symbol_{n}`" for n in range(1, 8))
    _write(repo, "docs/STATE.md", f"# State\n\nSee `services/many.py`: {symbols}.\n")
    _commit(repo, "docs: cite them all")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "TRUNCATED:" in result.stdout
    assert "windows per file" in result.stdout


def test_a_url_is_not_read_as_a_citation(tmp_path):
    repo, base = _base_repo(tmp_path)
    _write(
        repo,
        "docs/STATE.md",
        "# State\n\nUpstream: https://example.com/pkg/enrichment_service.py:21\n",
    )
    _commit(repo, "docs: link upstream")

    result = _run(repo, base)

    assert result.returncode == DOCS_ONLY, result.stderr
    assert "cites no source file" in result.stdout
    assert "UNRESOLVED" not in result.stdout


def test_an_empty_range_is_not_documentation_only(tmp_path):
    repo, base = _base_repo(tmp_path)

    result = _run(repo, base, head=base)

    assert result.returncode == NOT_DOCS_ONLY
    assert result.stdout == ""


def test_an_unknown_revision_is_an_error_not_a_pass(tmp_path):
    repo, base = _base_repo(tmp_path)

    result = _run(repo, base, head="0" * 40)

    assert result.returncode == 2, "a git failure must not read as documentation-only"
    assert result.stdout == ""
