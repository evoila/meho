# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard for the list of unit tests that read files outside ``backend/`` (#5296).

On a pull request that changes no file under ``backend/``, CI skips the unit
shards and runs only the files in ``OUTSIDE_BACKEND_TESTS``. A test that reads
a file outside ``backend/`` but is missing from the list would then not run,
and a pull request that breaks it would show green. So the list must match
what ``scan()`` finds, exactly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import _outside_backend_tests as outside


def test_list_matches_the_scan() -> None:
    listed = set(outside.OUTSIDE_BACKEND_TESTS)
    found = outside.scan()

    missing = sorted(found - listed)
    stale = sorted(listed - found)
    assert not missing, (
        "These test files read files outside backend/ but are not in "
        "OUTSIDE_BACKEND_TESTS (backend/tests/_outside_backend_tests.py). Add them: "
        f"{missing}"
    )
    assert not stale, (
        "These files in OUTSIDE_BACKEND_TESTS no longer read files outside "
        f"backend/, or were removed. Take them off the list: {stale}"
    )


def test_list_is_sorted_and_has_no_duplicates() -> None:
    files = list(outside.OUTSIDE_BACKEND_TESTS)
    assert files == sorted(set(files))


def test_every_listed_file_exists() -> None:
    for name in outside.OUTSIDE_BACKEND_TESTS:
        assert (outside.BACKEND_DIR / name).is_file(), name


# --- The scanner itself, on a fake backend/ tree -----------------------------


def _fake_backend(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``tmp_path/backend/tests``.

    The sources spell the two patterns the scan looks for a little
    differently (``FILE``, and a space before the bracket of the helper
    call). This writer puts the real spelling back, so the scan of the real
    test tree does not find this test file itself.
    """
    tests_dir = tmp_path / "backend" / "tests"
    for name, source in files.items():
        path = tests_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        real = source.replace("FILE", "__file__").replace("_repo_root (", "_repo_root" + "(")
        path.write_text(real, encoding="utf-8")
    return tests_dir


@pytest.mark.parametrize(
    ("name", "source", "reads_outside"),
    [
        # backend/tests/x.py: parents[2] is the repository root.
        ("test_a.py", 'R = Path(FILE).resolve().parents[2] / "deploy"\n', True),
        ("test_a.py", 'R = Path(FILE).resolve().parents[1] / "src"\n', False),
        ("test_a.py", 'R = Path(FILE).parent.parent.parent / "cli"\n', True),
        ("test_a.py", 'R = Path(FILE).parent.parent / "alembic"\n', False),
        ("test_a.py", "ROOT = _repo_root (Path(FILE))\n", True),
        # One folder deeper, the root is one level further up.
        ("acceptance/test_a.py", 'R = Path(FILE).resolve().parents[2] / "src"\n', False),
        ("acceptance/test_a.py", 'R = Path(FILE).resolve().parents[3] / "docs"\n', True),
        # Integration and migration tests are not part of the unit shards.
        ("integration/test_a.py", 'R = Path(FILE).resolve().parents[3] / "cli"\n', False),
        ("migrations/test_a.py", 'R = Path(FILE).resolve().parents[3] / "ci"\n', False),
    ],
)
def test_scan_finds_reads_outside_backend(
    tmp_path: Path, name: str, source: str, reads_outside: bool
) -> None:
    tests_dir = _fake_backend(tmp_path, {name: source})
    expected = {f"tests/{name}"} if reads_outside else set()
    assert outside.scan(tests_dir) == expected


def test_scan_follows_helper_modules(tmp_path: Path) -> None:
    tests_dir = _fake_backend(
        tmp_path,
        {
            "_helper.py": 'VALUES = Path(FILE).resolve().parents[2] / "deploy"\n',
            "test_uses_helper.py": "from tests._helper import VALUES\n",
            "test_plain.py": "import json\n",
        },
    )
    assert outside.scan(tests_dir) == {"tests/test_uses_helper.py"}
