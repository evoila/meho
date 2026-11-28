# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The unit test files that read files outside ``backend/``.

Some backend tests read files from other parts of the repository: the Helm
chart under ``deploy/``, the Go CLI under ``cli/``, the workflows under
``.github/``, the docs, the examples. A pull request that changes no file
under ``backend/`` can still break them.

CI skips the six unit shards on such a pull request (#5296). It runs only
the files that ``scan()`` finds, in the job ``python-outside-backend`` of
``.github/workflows/ci.yml``. The merge queue always runs every test.

There is no hand-kept list: ``scan()`` works the files out from their
source, so a new test file needs no edit here.
``test_outside_backend_tests.py`` checks the scan itself.

What ``scan()`` counts as "reads outside ``backend/``":

* ``Path(__file__)`` followed by ``.parents[N]`` or a chain of ``.parent``
  that climbs to the repository root (one level above ``backend/``);
* a call to a local helper named ``_repo_root``;
* an import of a helper module in ``tests/`` that does one of the two
  above (for example ``tests/_pgvector_image.py``, which reads the chart's
  ``values.yaml``).

Only the files the unit shards run are scanned: everything under ``tests/``
except ``tests/integration/`` and ``tests/migrations/``. The migration tests
have their own change detector. The integration tests need containers, so
the merge queue runs them.

CI usage::

    uv run python tests/_outside_backend_tests.py   # one path per line

This is CI tooling, not a test (the leading underscore keeps pytest from
collecting it).
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent

# `Path(__file__)`, an optional `.resolve()`, then `.parents[N]` or `.parent`s.
_FILE_PARENTS = re.compile(
    r"__file__\)(?:\.resolve\(\))?(?P<chain>(?:\.parent\b)+|\.parents\[(?P<index>\d+)\])"
)
_REPO_ROOT_CALL = re.compile(r"\b_repo_root\(")


def _scanned_files(tests_dir: Path) -> list[Path]:
    """Every ``.py`` file the unit shards can load: no integration, no migrations."""
    return sorted(
        path
        for path in tests_dir.rglob("*.py")
        if path.relative_to(tests_dir).parts[0] not in ("integration", "migrations")
    )


def _climbs_to_repo_root(path: Path, backend_dir: Path) -> bool:
    """True when ``path`` reaches a file one level above ``backend/``."""
    # backend/tests/x.py: parents[0] is tests/, parents[1] is backend/,
    # parents[2] is the repository root. One more level per subfolder.
    root_index = len(path.relative_to(backend_dir).parts)
    source = path.read_text(encoding="utf-8")
    for match in _FILE_PARENTS.finditer(source):
        if match.group("index") is not None:
            index = int(match.group("index"))
        else:
            index = match.group("chain").count(".parent") - 1
        if index >= root_index:
            return True
    return bool(_REPO_ROOT_CALL.search(source))


def scan(tests_dir: Path = TESTS_DIR) -> set[str]:
    """Return the test files that read outside ``backend/``, relative to it."""
    backend_dir = tests_dir.parent
    files = _scanned_files(tests_dir)
    helpers = {
        path.stem
        for path in files
        if not path.name.startswith("test_") and _climbs_to_repo_root(path, backend_dir)
    }
    found: set[str] = set()
    for path in files:
        if not path.name.startswith("test_"):
            continue
        source = path.read_text(encoding="utf-8")
        imports_helper = any(
            re.search(rf"^\s*(?:from|import)\s.*\b{re.escape(name)}\b", source, re.MULTILINE)
            for name in helpers
        )
        if imports_helper or _climbs_to_repo_root(path, backend_dir):
            found.add(str(path.relative_to(backend_dir)))
    return found


if __name__ == "__main__":
    print("\n".join(sorted(scan())))
