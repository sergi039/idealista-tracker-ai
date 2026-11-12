"""Where the committed reference files are read from (issue #639).

Four files are committed on purpose (`docs/rules/reference-data.md`): INE's
municipal figures, the national hospital catalogue, SEPE's registered
unemployment and the hand-curated agency table. They live in `data/` in the
repository, which is also the directory the deployment mounts its state at --
and that coincidence was the only way they reached the container:
`.dockerignore` excluded `data/` from the image, so the app read them through
the bind mount, out of the checkout.

Once the state directory can be somewhere other than the checkout, that stops
working: `/app/data` becomes a directory that holds a mail cursor and no
reference files. So the image carries its own copy, outside `/app/data`, and
says where with `REFERENCE_DATA_DIR` (Dockerfile). Outside the image -- a
checkout, pytest, an importer writing a new edition -- the variable is unset
and the answer is the repository's own `data/`, as it always was.

This module is the one place that knows either fact. A reader asks
`reference_path(name)`; `REFERENCE_FILES` is the list the Dockerfile,
`.dockerignore` and `.gitignore` each have to agree with, and
`tests/test_reference_data_location.py` holds them to it.

Deliberately not `Config.DATA_DIR`: that is the *state* directory, and a
reference file found there would be whatever a past deployment left behind
rather than what this build was reviewed with.
"""

from __future__ import annotations

import os

#: The committed reference files, by name. Order is the order `.gitignore`
#: re-includes them in.
REFERENCE_FILES = (
    "ine_municipal.json",
    "hospitals_cnh.json",
    "sepe_unemployment.json",
    "top_agencies.json",
)

#: Set by the image (Dockerfile). Unset everywhere else.
ENV_VARIABLE = "REFERENCE_DATA_DIR"

_CHECKOUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)


def reference_dir() -> str:
    """The directory this process reads reference files from.

    An empty variable is unset: `REFERENCE_DATA_DIR=` in an env file must not
    turn every path into a root-relative one.
    """
    return os.environ.get(ENV_VARIABLE, "").strip() or _CHECKOUT_DIR


def reference_path(name: str) -> str:
    """The path of one committed reference file.

    Only the four names above. Anything else is a programming error, and an
    answer for it would be a path into a directory that is not promised to
    hold anything else.
    """
    if name not in REFERENCE_FILES:
        raise ValueError(
            f"{name!r} is not a committed reference file; known: "
            + ", ".join(REFERENCE_FILES)
        )
    return os.path.join(reference_dir(), name)
