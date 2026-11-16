# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Checks for the two upgrade tables and the upgrade row files.

The tables are in ``docs/deploying.md`` and ``docs-site/install/upgrades.md``.
New rows are one small file each in ``docs/upgrade-rows/`` and are added to
the site table at build time (``scripts/ci/upgrade_rows.py``). Two pull
requests that each add a row never touch the same file.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# backend/tests/<this> -> parents[2] == repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "upgrade_rows.py"

_spec = importlib.util.spec_from_file_location("upgrade_rows", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ur = importlib.util.module_from_spec(_spec)
sys.modules["upgrade_rows"] = ur
_spec.loader.exec_module(ur)

_HEADING = "## Version-specific notes"
_TABLE = (
    "# Page\n\n"
    f"{_HEADING}\n\n"
    "| Crossing | What changes | Action |\n"
    "|---|---|---|\n"
    "| **v0.1.0** | A note. | An action. |\n"
    "\n"
    "After the table.\n"
)


def _row_file(root: Path, version: str, name: str, body: str) -> Path:
    path = root / version / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


_GOOD = "## Note\nWhat changes.\n\n## Action\nWhat to do.\n"


def test_the_real_tables_and_rows_pass() -> None:
    ur.check_table_files()
    ur.collect_rows()
    ur.check_released_rows()


def test_script_exits_zero_on_the_repo() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("marker", ["<<<<<<< HEAD", "=======", ">>>>>>> main"])
def test_table_with_a_conflict_marker_fails(marker: str) -> None:
    text = _TABLE.replace("After the table.", marker)
    with pytest.raises(ur.UpgradeRowError, match="conflict marker"):
        ur.check_table_text(text, _HEADING, "page.md")


def test_marker_in_the_middle_of_the_table_fails() -> None:
    text = _TABLE.replace("|---|---|---|\n", "|---|---|---|\n<<<<<<< HEAD\n")
    with pytest.raises(ur.UpgradeRowError, match="conflict marker"):
        ur.check_table_text(text, _HEADING, "page.md")


def test_doubled_row_fails() -> None:
    row = "| **v0.1.0** | A note. | An action. |\n"
    with pytest.raises(ur.UpgradeRowError, match="twice"):
        ur.check_table_text(_TABLE.replace(row, row + row), _HEADING, "page.md")


@pytest.mark.parametrize(
    "row",
    [
        "| **v0.2.0** | Only two. |",
        "| **v0.2.0** | a | b | c |",
        "| **v0.2.0** | a | b",
        "| **v0.2.0** | A | B | C",
        "| **v0.2.0** | A | B \\|",
    ],
)
def test_row_with_wrong_column_count_fails(row: str) -> None:
    text = _TABLE.replace("\nAfter", f"{row}\n\nAfter")
    with pytest.raises(ur.UpgradeRowError, match="columns"):
        ur.check_table_text(text, _HEADING, "page.md")


def test_escaped_pipe_is_one_cell_character() -> None:
    text = _TABLE.replace("A note.", r"A \| note.")
    ur.check_table_text(text, _HEADING, "page.md")


def test_missing_heading_fails() -> None:
    with pytest.raises(ur.UpgradeRowError, match="not found"):
        ur.check_table_text("# Page\n", _HEADING, "page.md")


def test_rows_are_read_and_sorted_by_version_not_by_text(tmp_path: Path) -> None:
    _row_file(tmp_path, "v0.10.0", "a", _GOOD.replace("What changes.", "Ten."))
    _row_file(tmp_path, "v0.9.0", "b", _GOOD.replace("What changes.", "Nine."))
    rows = ur.collect_rows(tmp_path)
    assert [r.version for r in rows] == ["v0.9.0", "v0.10.0"]
    assert rows[0].as_table_line() == "| **v0.9.0** | Nine. | What to do. |"


def test_two_rows_for_one_version_do_not_touch_the_same_file(tmp_path: Path) -> None:
    _row_file(tmp_path, "v0.2.0", "first", _GOOD)
    _row_file(tmp_path, "v0.2.0", "second", _GOOD.replace("What to do.", "Other."))
    assert [r.name for r in ur.collect_rows(tmp_path)] == ["first", "second"]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("## Note\nOnly a note.\n", "Action"),
        ("## Note\nA\nB\n\n## Action\nC\n", "exactly one line"),
        ("## Note\nA\n\n## Action\nC\n\n## Action\nD\n", "twice"),
        ("## Note\nA | B\n\n## Action\nC\n", r"\|"),
        ("## Note\nA\n\n## Other\nC\n", "unknown section"),
        ("Stray text\n## Note\nA\n\n## Action\nC\n", "before the first"),
        ("## Note\n<<<<<<< HEAD\n\n## Action\nC\n", "conflict marker"),
        ("## Note\nA\n\n## Action\nSee [setup](keycloak-realm.md).\n", "keycloak-realm.md"),
        ("## Note\nA\n\n## Action\nSee [it](../guides/x.md#a).\n", "full address"),
        ("## Note\nSee [above](#upgrading).\n\n## Action\nC\n", "full address"),
        ("## Note\nA\n\n## Action\nSee [it]( <docs/x.md> ).\n", "full address"),
        ("## Note\nA\n\n## Action\nSee [it](http://x.example/a).\n", "full address"),
        ("## Note\nA\n\n## Action\nSee [it][ref].\n", "reference link"),
    ],
)
def test_broken_row_file_fails(tmp_path: Path, body: str, match: str) -> None:
    _row_file(tmp_path, "v0.2.0", "bad", body)
    with pytest.raises(ur.UpgradeRowError, match=match):
        ur.collect_rows(tmp_path)


def test_row_file_with_a_bad_folder_or_file_name_fails(tmp_path: Path) -> None:
    _row_file(tmp_path, "next", "x", _GOOD)
    with pytest.raises(ur.UpgradeRowError, match="version"):
        ur.collect_rows(tmp_path)


def test_row_file_that_is_not_markdown_fails(tmp_path: Path) -> None:
    (tmp_path / "v0.2.0").mkdir()
    (tmp_path / "v0.2.0" / "x.txt").write_text("x")
    with pytest.raises(ur.UpgradeRowError, match=r"\.md"):
        ur.collect_rows(tmp_path)


def test_same_text_in_two_files_of_one_version_fails(tmp_path: Path) -> None:
    _row_file(tmp_path, "v0.2.0", "one", _GOOD)
    _row_file(tmp_path, "v0.2.0", "two", _GOOD)
    with pytest.raises(ur.UpgradeRowError, match="same text"):
        ur.collect_rows(tmp_path)


def test_rows_are_added_at_the_end_of_the_table(tmp_path: Path) -> None:
    _row_file(tmp_path, "v0.2.0", "x", _GOOD)
    out = ur.add_rows_to_table(_TABLE, _HEADING, ur.collect_rows(tmp_path))
    lines = out.splitlines()
    assert lines[lines.index("| **v0.1.0** | A note. | An action. |") + 1] == (
        "| **v0.2.0** | What changes. | What to do. |"
    )
    assert out.endswith("After the table.\n")
    ur.check_table_text(out, _HEADING, "page.md")


def test_a_row_already_in_the_table_fails(tmp_path: Path) -> None:
    _row_file(tmp_path, "v0.1.0", "x", "## Note\nA note.\n\n## Action\nAn action.\n")
    with pytest.raises(ur.UpgradeRowError, match="already holds"):
        ur.add_rows_to_table(_TABLE, _HEADING, ur.collect_rows(tmp_path))


def test_no_rows_leaves_the_page_unchanged() -> None:
    assert ur.add_rows_to_table(_TABLE, _HEADING, []) == _TABLE


def _repo_copy(root: Path, changelog: str) -> Path:
    """A small repo: the two tables, a CHANGELOG and an empty rows folder."""
    for rel, heading in ur.TABLE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TABLE.replace(_HEADING, heading), encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    rows_dir = root / "docs" / "upgrade-rows"
    rows_dir.mkdir(parents=True)
    return rows_dir


class _Page:
    class file:  # noqa: N801 - the shape mkdocs passes to the hook
        src_uri = ur.SITE_PAGE


def test_a_new_row_file_reaches_both_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, "## [Unreleased]\n")
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    line = "| **v0.2.0** | What changes. | What to do. |"
    monkeypatch.setattr(ur, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ur, "ROWS_DIR", rows_dir)

    site = ur.on_page_markdown(_TABLE, _Page(), None, None)
    assert line in site.splitlines()

    deploying = tmp_path / ur.DEPLOYING
    assert [r.name for r in ur.write_deploying(tmp_path, rows_dir)] == ["x"]
    assert line in deploying.read_text(encoding="utf-8").splitlines()
    ur.check_table_files(tmp_path)

    before = deploying.read_text(encoding="utf-8")
    assert ur.write_deploying(tmp_path, rows_dir) == []
    assert deploying.read_text(encoding="utf-8") == before


def test_a_released_row_missing_in_deploying_fails(tmp_path: Path) -> None:
    rows_dir = _repo_copy(tmp_path, "## [Unreleased]\n\n## [0.2.0] - 2026-01-01\n")
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    with pytest.raises(ur.UpgradeRowError, match=r"v0\.2\.0/x\.md.*--write"):
        ur.check_released_rows(tmp_path, rows_dir)
    ur.write_deploying(tmp_path, rows_dir)
    ur.check_released_rows(tmp_path, rows_dir)


def test_an_unreleased_row_need_not_be_in_deploying(tmp_path: Path) -> None:
    rows_dir = _repo_copy(tmp_path, "## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n")
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    ur.check_released_rows(tmp_path, rows_dir)


def test_a_linked_row_reaches_both_tables_with_the_same_full_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, "## [Unreleased]\n")
    url = "https://evoila.github.io/meho/latest/install/keycloak-realm/#the-approval-level"
    _row_file(rows_dir, "v0.2.0", "x", f"## Note\nA change.\n\n## Action\nSee [setup]({url}).\n")
    line = f"| **v0.2.0** | A change. | See [setup]({url}). |"
    monkeypatch.setattr(ur, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ur, "ROWS_DIR", rows_dir)

    assert line in ur.on_page_markdown(_TABLE, _Page(), None, None).splitlines()
    ur.write_deploying(tmp_path, rows_dir)
    assert line in (tmp_path / ur.DEPLOYING).read_text(encoding="utf-8").splitlines()
