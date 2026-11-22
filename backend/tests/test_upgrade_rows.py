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
    _row_file(tmp_path / "rows", "v0.10.0", "a", _GOOD.replace("What changes.", "Ten."))
    _row_file(tmp_path / "rows", "v0.9.0", "b", _GOOD.replace("What changes.", "Nine."))
    rows = ur.collect_rows(tmp_path / "rows")
    assert [r.version for r in rows] == ["v0.9.0", "v0.10.0"]
    assert rows[0].as_table_line() == "| **v0.9.0** | Nine. | What to do. |"


def test_two_rows_for_one_version_do_not_touch_the_same_file(tmp_path: Path) -> None:
    _row_file(tmp_path / "rows", "v0.2.0", "first", _GOOD)
    _row_file(tmp_path / "rows", "v0.2.0", "second", _GOOD.replace("What to do.", "Other."))
    assert [r.name for r in ur.collect_rows(tmp_path / "rows")] == ["first", "second"]


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
    _row_file(tmp_path / "rows", "v0.2.0", "bad", body)
    with pytest.raises(ur.UpgradeRowError, match=match):
        ur.collect_rows(tmp_path / "rows")


def test_row_file_with_a_bad_folder_or_file_name_fails(tmp_path: Path) -> None:
    _row_file(tmp_path / "rows", "next", "x", _GOOD)
    with pytest.raises(ur.UpgradeRowError, match="version"):
        ur.collect_rows(tmp_path / "rows")


def test_row_file_that_is_not_markdown_fails(tmp_path: Path) -> None:
    (tmp_path / "rows" / "v0.2.0").mkdir(parents=True)
    (tmp_path / "rows" / "v0.2.0" / "x.txt").write_text("x")
    with pytest.raises(ur.UpgradeRowError, match=r"\.md"):
        ur.collect_rows(tmp_path / "rows")


def test_same_text_in_two_files_of_one_version_fails(tmp_path: Path) -> None:
    _row_file(tmp_path / "rows", "v0.2.0", "one", _GOOD)
    _row_file(tmp_path / "rows", "v0.2.0", "two", _GOOD)
    with pytest.raises(ur.UpgradeRowError, match="same text"):
        ur.collect_rows(tmp_path / "rows")


def test_rows_are_added_at_the_end_of_the_table(tmp_path: Path) -> None:
    _row_file(tmp_path / "rows", "v0.2.0", "x", _GOOD)
    out = ur.add_rows_to_table(_TABLE, _HEADING, ur.collect_rows(tmp_path / "rows"))
    lines = out.splitlines()
    assert lines[lines.index("| **v0.1.0** | A note. | An action. |") + 1] == (
        "| **v0.2.0** | What changes. | What to do. |"
    )
    assert out.endswith("After the table.\n")
    ur.check_table_text(out, _HEADING, "page.md")


def test_a_row_already_in_the_table_fails(tmp_path: Path) -> None:
    _row_file(tmp_path / "rows", "v0.1.0", "x", "## Note\nA note.\n\n## Action\nAn action.\n")
    with pytest.raises(ur.UpgradeRowError, match="already holds"):
        ur.add_rows_to_table(_TABLE, _HEADING, ur.collect_rows(tmp_path / "rows"))


def test_no_rows_leaves_the_page_unchanged() -> None:
    assert ur.add_rows_to_table(_TABLE, _HEADING, []) == _TABLE


def _repo_copy(root: Path, changelog: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small repo: the two tables (one frozen row each), a CHANGELOG, a rows folder."""
    for rel, heading in ur.TABLE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_TABLE.replace(_HEADING, heading), encoding="utf-8")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    rows_dir = root / "docs" / "upgrade-rows"
    rows_dir.mkdir(parents=True)
    (rows_dir / "README.md").write_text("# Upgrade rows\n", encoding="utf-8")
    monkeypatch.setattr(ur, "FROZEN_ROWS", dict.fromkeys(ur.TABLE_FILES, 1))
    monkeypatch.setattr(ur, "REPO_ROOT", root)
    monkeypatch.setattr(ur, "ROWS_DIR", rows_dir)
    return rows_dir


class _Page:
    class file:  # noqa: N801 - the shape mkdocs passes to the hook
        src_uri = ur.SITE_PAGE


_LINE = "| **v0.2.0** | What changes. | What to do. |"
_UNRELEASED = "## [Unreleased]\n\n## [0.1.0] - 2026-01-01\n"
_RELEASED = "## [Unreleased]\n\n## [0.2.0] - 2026-02-01\n\n## [0.1.0] - 2026-01-01\n"


def _deploying_lines(root: Path) -> list[str]:
    return (root / ur.DEPLOYING).read_text(encoding="utf-8").splitlines()


def _release(root: Path) -> None:
    (root / "CHANGELOG.md").write_text(_RELEASED, encoding="utf-8")


def test_a_new_row_file_reaches_both_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _UNRELEASED, monkeypatch)
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)

    # Before the release: the site shows the row at once, --write adds nothing.
    assert _LINE in ur.on_page_markdown(_TABLE, _Page(), None, None).splitlines()
    assert ur.write_deploying(tmp_path, rows_dir) == []
    assert _LINE not in _deploying_lines(tmp_path)

    # The release pull request rolls the CHANGELOG and runs --write.
    _release(tmp_path)
    assert [r.name for r in ur.write_deploying(tmp_path, rows_dir)] == ["x"]
    assert _LINE in _deploying_lines(tmp_path)
    ur.check_all(tmp_path, rows_dir)

    before = _deploying_lines(tmp_path)
    assert ur.write_deploying(tmp_path, rows_dir) == []
    assert _deploying_lines(tmp_path) == before


def test_a_linked_row_reaches_both_tables_with_the_same_full_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    url = "https://evoila.github.io/meho/latest/install/keycloak-realm/#the-approval-level"
    _row_file(rows_dir, "v0.2.0", "x", f"## Note\nA change.\n\n## Action\nSee [setup]({url}).\n")
    line = f"| **v0.2.0** | A change. | See [setup]({url}). |"

    ur.write_deploying(tmp_path, rows_dir)
    assert line in _deploying_lines(tmp_path)
    assert line in ur.on_page_markdown(_TABLE, _Page(), None, None).splitlines()


def test_a_released_row_missing_in_deploying_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    with pytest.raises(ur.UpgradeRowError, match=r"v0\.2\.0/x\.md.*--write") as err:
        ur.check_released_rows(tmp_path, rows_dir)
    # The same state is a late row for a release that had no row files.
    assert "v0.2.0 is already released; put the row in the folder of the next release" in str(
        err.value
    )
    ur.write_deploying(tmp_path, rows_dir)
    ur.check_released_rows(tmp_path, rows_dir)


def test_the_docs_build_fails_while_a_released_row_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mkdocs.exceptions import PluginError

    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    with pytest.raises(PluginError, match="--write"):
        ur.on_page_markdown(_TABLE, _Page(), None, None)


def test_an_unreleased_row_need_not_be_in_deploying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _UNRELEASED, monkeypatch)
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    ur.check_all(tmp_path, rows_dir)


def test_a_new_row_of_an_older_release_fails_without_write_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    _row_file(rows_dir, "v0.1.0", "late", _GOOD)
    for call in (ur.check_released_rows, ur.write_deploying):
        with pytest.raises(ur.UpgradeRowError, match="already released") as err:
            call(tmp_path, rows_dir)
        assert "--write" not in str(err.value)
    assert _LINE.replace("v0.2.0", "v0.1.0") not in _deploying_lines(tmp_path)


def test_a_new_row_of_the_newest_release_after_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    ur.write_deploying(tmp_path, rows_dir)
    _row_file(rows_dir, "v0.2.0", "y", "## Note\nLate.\n\n## Action\nLate.\n")
    with pytest.raises(ur.UpgradeRowError, match=r"v0\.2\.0/y\.md: v0\.2\.0 is already released"):
        ur.check_released_rows(tmp_path, rows_dir)


@pytest.mark.parametrize("rel", list(ur.TABLE_FILES))
def test_a_row_added_by_hand_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    rows_dir = _repo_copy(tmp_path, _UNRELEASED, monkeypatch)
    path = tmp_path / rel
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("| An action. |\n", "| An action. |\n| **v0.2.0** | Hand. | Hand. |\n"),
        encoding="utf-8",
    )
    with pytest.raises(ur.UpgradeRowError, match=rf"{rel}:\d+: this row has no row file"):
        ur.check_all(tmp_path, rows_dir)


def test_a_removed_frozen_row_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows_dir = _repo_copy(tmp_path, _UNRELEASED, monkeypatch)
    path = tmp_path / ur.DEPLOYING
    path.write_text(
        path.read_text(encoding="utf-8").replace("| **v0.1.0** | A note. | An action. |\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(ur.UpgradeRowError, match="do not remove rows"):
        ur.check_frozen_tables(tmp_path, rows_dir)


def test_an_edited_row_file_after_write_needs_the_same_edit_in_deploying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    row = _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    ur.write_deploying(tmp_path, rows_dir)
    row.write_text(_GOOD.replace("What to do.", "What to do now."), encoding="utf-8")
    with pytest.raises(
        ur.UpgradeRowError,
        match=r"docs/deploying\.md:\d+: this row differs from v0\.2\.0/x\.md; "
        "change both in the same pull request",
    ):
        ur.check_all(tmp_path, rows_dir)

    path = tmp_path / ur.DEPLOYING
    path.write_text(
        path.read_text(encoding="utf-8").replace("What to do.", "What to do now."),
        encoding="utf-8",
    )
    ur.check_all(tmp_path, rows_dir)


def test_a_file_outside_a_version_folder_fails(tmp_path: Path) -> None:
    (tmp_path / "rows").mkdir()
    (tmp_path / "rows" / "README.md").write_text("# Rows\n", encoding="utf-8")
    assert ur.collect_rows(tmp_path / "rows") == []
    (tmp_path / "rows" / "my-row.md").write_text(_GOOD, encoding="utf-8")
    with pytest.raises(ur.UpgradeRowError, match="belongs in a version folder"):
        ur.collect_rows(tmp_path / "rows")


def test_an_edited_written_row_in_deploying_names_its_row_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows_dir = _repo_copy(tmp_path, _RELEASED, monkeypatch)
    _row_file(rows_dir, "v0.2.0", "x", _GOOD)
    ur.write_deploying(tmp_path, rows_dir)
    path = tmp_path / ur.DEPLOYING
    path.write_text(
        path.read_text(encoding="utf-8").replace("What to do.", "Edited here."),
        encoding="utf-8",
    )
    with pytest.raises(ur.UpgradeRowError, match=r"this row differs from v0\.2\.0/x\.md") as err:
        ur.check_all(tmp_path, rows_dir)
    assert "Move your row" not in str(err.value)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose base commit releases 0.47.0 and holds one old row."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [0.47.0]\n", encoding="utf-8")
    _row_file(tmp_path / "docs" / "upgrade-rows", "v0.47.0", "old", _GOOD)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "pr")
    return tmp_path


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


def test_new_row_in_released_folder_fails(repo: Path) -> None:
    _row_file(repo / "docs" / "upgrade-rows", "v0.47.0", "new", _GOOD)
    _commit(repo)
    with pytest.raises(ur.UpgradeRowError, match=r"v0.47.0 is released.*git mv"):
        ur.check_new_rows("main", repo)


def test_new_row_in_next_folder_passes(repo: Path) -> None:
    _row_file(repo / "docs" / "upgrade-rows", "v0.48.0", "new", _GOOD)
    _commit(repo)
    assert ur.check_new_rows("main", repo) == ["docs/upgrade-rows/v0.48.0/new.md"]


def test_move_from_released_folder_to_next_passes(repo: Path) -> None:
    next_dir = repo / "docs" / "upgrade-rows" / "v0.48.0"
    next_dir.mkdir(parents=True)
    _git(repo, "mv", "docs/upgrade-rows/v0.47.0/old.md", "docs/upgrade-rows/v0.48.0/old.md")
    _commit(repo)
    assert ur.check_new_rows("main", repo) == ["docs/upgrade-rows/v0.48.0/old.md"]


def test_old_row_in_released_folder_passes(repo: Path) -> None:
    (repo / "docs" / "upgrade-rows" / "v0.47.0" / "old.md").write_text(
        "## Note\nFixed typo.\n\n## Action\nWhat to do.\n", encoding="utf-8"
    )
    _commit(repo)
    assert ur.check_new_rows("main", repo) == []


def test_release_pull_request_passes(repo: Path) -> None:
    # The head adds the heading for 0.48.0 and the row folder of 0.48.0 at once.
    _row_file(repo / "docs" / "upgrade-rows", "v0.48.0", "new", _GOOD)
    (repo / "CHANGELOG.md").write_text("## [0.48.0]\n\n## [0.47.0]\n", encoding="utf-8")
    _commit(repo)
    assert ur.check_new_rows("main", repo) == ["docs/upgrade-rows/v0.48.0/new.md"]
