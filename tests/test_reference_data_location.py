"""The committed reference files have one home, and four lists agree on which
files they are (issue #639).

`data/ine_municipal.json`, `data/hospitals_cnh.json`,
`data/sepe_unemployment.json` and `data/top_agencies.json` are committed on
purpose (`docs/rules/reference-data.md`). They sit in the directory the
deployment mounts its state at, and until #639 that mount was the only way
they reached the container: `.dockerignore` excluded `data/` from the image.

Once the state directory can live outside the checkout, `/app/data` no longer
holds them. So the image carries its own copy at `REFERENCE_DATA_DIR` and
every reader asks `utils/reference_data.py`. That is four places naming the
same four files -- `.gitignore` (tracked), `.dockerignore` (in the build
context), the Dockerfile (copied out of the mount's way) and the Python list
(what a reader may ask for). A fifth file added to three of them is a file
that is committed, reviewed and silently absent in production, which reads as
`no_reference_data` on a page rather than as an error anywhere.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from utils import reference_data
from utils.reference_data import REFERENCE_FILES, reference_dir, reference_path

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE_DIR = "/opt/idealista/reference_data"


def _negations(name: str) -> list[str]:
    """`!data/<file>` lines of an ignore file, in order."""
    lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
    return [
        line.strip()[len("!data/") :]
        for line in lines
        if line.strip().startswith("!data/")
    ]


def _patterns(name: str) -> list[str]:
    lines = (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


class TestFourListsOneSet:
    def test_gitignore_re_includes_exactly_these(self):
        assert _negations(".gitignore") == list(REFERENCE_FILES)

    def test_dockerignore_re_includes_exactly_these(self):
        assert _negations(".dockerignore") == list(REFERENCE_FILES)

    def test_dockerignore_excludes_the_rest_of_the_directory(self):
        """`data/*`, not `data/`: the bare-directory form makes every
        re-include dead, and no form at all would bake the deployment's state
        into an image built on the mini."""
        patterns = _patterns(".dockerignore")
        assert "data/*" in patterns
        assert "data/" not in patterns and "data" not in patterns
        assert patterns.index("data/*") < patterns.index(f"!data/{REFERENCE_FILES[0]}")

    def test_the_dockerfile_copies_exactly_these_out_of_the_mounts_way(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        match = re.search(
            r"^COPY --chown=appuser:appuser \\\n((?:\s+data/\S+ \\\n)+)\s+(\S+)\n",
            dockerfile,
            flags=re.MULTILINE,
        )
        assert match, "the reference-file COPY is not in the Dockerfile"
        copied = [
            line.strip().removesuffix("\\").strip().removeprefix("data/")
            for line in match.group(1).splitlines()
        ]
        assert copied == list(REFERENCE_FILES)
        assert match.group(2) == f"{IMAGE_DIR}/"

    def test_the_image_says_where_and_after_the_copy(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        env_line = f"ENV {reference_data.ENV_VARIABLE}={IMAGE_DIR}"
        assert env_line in dockerfile
        assert dockerfile.index(f"{IMAGE_DIR}/\n") < dockerfile.index(env_line)

    def test_the_image_directory_is_not_under_the_state_mount(self):
        """Anything under /app/data is hidden by the bind mount at run time,
        which is the whole reason for the second copy."""
        assert not IMAGE_DIR.startswith("/app/data")

    def test_every_listed_file_is_committed(self):
        for name in REFERENCE_FILES:
            assert (REPO_ROOT / "data" / name).is_file(), name


class TestTheOneHome:
    def test_a_checkout_reads_its_own_data_directory(self, monkeypatch):
        monkeypatch.delenv(reference_data.ENV_VARIABLE, raising=False)
        assert reference_dir() == str(REPO_ROOT / "data")
        assert reference_path("ine_municipal.json") == str(
            REPO_ROOT / "data" / "ine_municipal.json"
        )

    def test_the_image_reads_the_directory_it_names(self, monkeypatch):
        monkeypatch.setenv(reference_data.ENV_VARIABLE, IMAGE_DIR)
        assert reference_dir() == IMAGE_DIR
        assert reference_path("top_agencies.json") == f"{IMAGE_DIR}/top_agencies.json"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_an_empty_variable_is_unset(self, monkeypatch, value):
        """`REFERENCE_DATA_DIR=` must not turn every path into `/<file>`."""
        monkeypatch.setenv(reference_data.ENV_VARIABLE, value)
        assert reference_dir() == str(REPO_ROOT / "data")

    def test_it_does_not_follow_the_state_directory(self, monkeypatch, tmp_path):
        """`DATA_DIR` is where the mail cursor is. A reference file found
        there is whatever a past deployment left behind."""
        monkeypatch.delenv(reference_data.ENV_VARIABLE, raising=False)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert reference_dir() == str(REPO_ROOT / "data")

    def test_a_name_that_is_not_a_reference_file_is_refused(self):
        with pytest.raises(ValueError, match="not a committed reference file"):
            reference_path("google_spend.jsonl")
        with pytest.raises(ValueError):
            reference_path("../.env")


class TestTheReadersAskIt:
    """Each reader's module constant is what `reference_path` answers, so a
    reader cannot keep a private idea of where the files are."""

    def test_quality_of_life(self):
        from services import quality_of_life_service as module

        assert module.INE_DATA_PATH == reference_path("ine_municipal.json")
        assert module.CNH_DATA_PATH == reference_path("hospitals_cnh.json")
        assert module.SEPE_DATA_PATH == reference_path("sepe_unemployment.json")

    def test_agencies(self):
        from services import agency_directory as module

        assert module.TOP_AGENCIES_PATH == reference_path("top_agencies.json")

    def test_municipality_codes(self):
        from utils import municipality_codes as module

        assert str(module._INE_DATA_PATH) == reference_path("ine_municipal.json")

    def test_no_application_module_builds_the_path_by_hand(self):
        """`os.path.join(<code root>, "data", "<reference file>")` in a reader
        is a reader that looks in the state mount inside the container."""
        offenders = []
        for folder in ("services", "routes", "utils"):
            for path in sorted((REPO_ROOT / folder).rglob("*.py")):
                if path.name in {"reference_data.py"}:
                    continue
                if path.name.startswith(("import_", "generate_")):
                    # Importers and generators write a new edition into the
                    # checkout, from an explicit `--out` path; not readers.
                    continue
                text = path.read_text(encoding="utf-8")
                for number, line in enumerate(text.splitlines(), start=1):
                    code = line.split("#", 1)[0]
                    if '"data"' not in code and "'data'" not in code:
                        continue
                    if any(name in code for name in REFERENCE_FILES):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
        assert offenders == []

    def test_the_files_are_readable_where_the_home_says(self):
        for name in REFERENCE_FILES:
            assert os.path.getsize(reference_path(name)) > 0
