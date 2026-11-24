#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Upgrade rows: one small file per row, and the checks for the two tables.

Every pull request that changes how an upgrade works adds a row to the
upgrade tables. If each pull request edits the same table, every merge of
main into an open pull request conflicts at the end of the table. So a new
row is its own file::

    docs/upgrade-rows/<version>/<short-name>.md

    ## Note
    What changes, in plain words.

    ## Action
    What the operator does.

Each section is one line of text. The version is the folder name, for
example ``v0.47.0``. A link in a row must be a full ``https://`` address:
the same row goes into two tables in two folders, so a relative link would
work in one of them only.

The rows sort by version, then by file name. A version is released when
``CHANGELOG.md`` has a ``## [X.Y.Z]`` heading for it.

This file does these things:

* ``collect_rows`` reads and checks the row files.
* ``check_table_files`` checks the two tables (``docs/deploying.md`` and
  ``docs-site/install/upgrades.md``): no conflict marker line, 3 columns in
  every row, no doubled row.
* ``check_frozen_tables`` fails on a row added by hand. The rows that were
  there before the row files came are frozen (``FROZEN_ROWS``). Below them,
  ``docs/deploying.md`` holds only rows that ``--write`` added.
* ``on_page_markdown`` is a ``mkdocs`` hook. It runs all checks and adds the
  rows of the folder to the end of the table of ``install/upgrades.md`` when
  the site is built. A broken row or table fails the build.
* ``write_deploying`` adds the rows of the newest released version that are
  not there yet to the end of the table in ``docs/deploying.md``. The release
  pull request runs it (``--write``), so contributors never edit that table.
  ``check_released_rows`` fails when such a row is missing there, and when a
  new row is in the folder of a version that was released before.
* ``check_new_rows`` (``--check-new-rows <base ref>``) runs in a pull request.
  It fails on a row file that the pull request adds to the folder of a version
  that is released on the base branch.

``python scripts/ci/upgrade_rows.py`` runs the checks and prints the rows as
table lines, so you can see what the site will show.
``python scripts/ci/upgrade_rows.py --write`` also adds the missing rows to
``docs/deploying.md``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ROWS_DIR = REPO_ROOT / "docs" / "upgrade-rows"

# The two hand-kept tables: file -> heading above the table.
TABLE_FILES: dict[str, str] = {
    "docs/deploying.md": "## Version-specific upgrade notes",
    "docs-site/install/upgrades.md": "## Version-specific notes",
}
DEPLOYING = "docs/deploying.md"
# Rows of each table before the row files came. They stay as they are; a new
# row is a row file.
FROZEN_ROWS: dict[str, int] = {
    "docs/deploying.md": 69,
    "docs-site/install/upgrades.md": 35,
}
_MOVE_HINT = (
    "Do not edit this table. Move your row to "
    "docs/upgrade-rows/<version>/<short-name>.md (see docs/upgrade-rows/README.md)."
)
SITE_PAGE = "install/upgrades.md"

_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_MARKER_RE = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")
_SECTIONS = ("Note", "Action")
_PIPE_RE = re.compile(r"(?<!\\)\|")
_RELEASED_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
# Inline link "](target" and reference link "][label]".
_LINK_RE = re.compile(r"\]\(\s*<?([^)\s>]*)|\]\[")
_LINK_HINT = (
    "write the full address, for example https://evoila.github.io/meho/latest/install/upgrades/ "
    "for a docs site page or https://github.com/evoila/meho/blob/main/docs/deploying.md "
    "for a repo file"
)


class UpgradeRowError(Exception):
    """A row file or an upgrade table is wrong."""


def _version_key(version: str) -> tuple[int, ...]:
    match = _VERSION_RE.match(version)
    assert match is not None
    return tuple(int(part) for part in match.groups())


@dataclass(frozen=True)
class Row:
    version: str
    name: str
    note: str
    action: str

    def sort_key(self) -> tuple[tuple[int, ...], str]:
        return _version_key(self.version), self.name

    def as_table_line(self) -> str:
        return f"| **{self.version}** | {self.note} | {self.action} |"


def _parse_row_file(path: Path, version: str, rows_dir: Path) -> Row:
    where = path.relative_to(rows_dir).as_posix()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if _MARKER_RE.match(line):
            raise UpgradeRowError(f"{where}: conflict marker line: {line!r}")
        if line.startswith("## "):
            current = line[3:].strip()
            if current not in _SECTIONS:
                raise UpgradeRowError(
                    f"{where}: unknown section {current!r}; use 'Note' and 'Action'"
                )
            if current in sections:
                raise UpgradeRowError(f"{where}: section {current!r} is there twice")
            sections[current] = []
        elif line.strip():
            if current is None:
                raise UpgradeRowError(f"{where}: text before the first '## ' heading")
            sections[current].append(line.strip())
    cells: dict[str, str] = {}
    for name in _SECTIONS:
        lines = sections.get(name, [])
        if len(lines) != 1:
            raise UpgradeRowError(
                f"{where}: section '{name}' must hold exactly one line of text (found {len(lines)})"
            )
        if _PIPE_RE.search(lines[0]):
            raise UpgradeRowError(f"{where}: section '{name}' holds a '|'; write '\\|'")
        for link in _LINK_RE.finditer(lines[0]):
            target = link.group(1)
            if target is None or not target.startswith("https://"):
                shown = "a reference link" if target is None else repr(target)
                raise UpgradeRowError(
                    f"{where}: section '{name}' has {shown}; a row goes into two "
                    f"tables in two folders, so {_LINK_HINT}"
                )
        cells[name] = lines[0]
    return Row(version, path.stem, cells["Note"], cells["Action"])


def collect_rows(rows_dir: Path = ROWS_DIR) -> list[Row]:
    """Read every row file, check it, and return the rows in version order."""
    rows: list[Row] = []
    if not rows_dir.is_dir():
        return rows
    for version_dir in sorted(rows_dir.iterdir()):
        if not version_dir.is_dir():
            if version_dir.name == "README.md":
                continue
            raise UpgradeRowError(
                f"{version_dir.name}: a row file belongs in a version folder, "
                f"for example v0.47.0/{version_dir.name}"
            )
        if not _VERSION_RE.match(version_dir.name):
            raise UpgradeRowError(
                f"{version_dir.name}: the folder name must be a version like v0.47.0"
            )
        for path in sorted(version_dir.iterdir()):
            if path.suffix != ".md" or not path.is_file():
                raise UpgradeRowError(
                    f"{path.relative_to(rows_dir).as_posix()}: only .md row files belong here"
                )
            rows.append(_parse_row_file(path, version_dir.name, rows_dir))
    seen: dict[tuple[str, str, str], str] = {}
    for row in rows:
        key = (row.version, row.note, row.action)
        if key in seen:
            raise UpgradeRowError(f"{row.version}/{row.name}.md has the same text as {seen[key]}")
        seen[key] = f"{row.version}/{row.name}.md"
    return sorted(rows, key=Row.sort_key)


def _table_block(lines: list[str], heading: str) -> tuple[int, int]:
    """Return (start, end) of the first table after ``heading``; end is exclusive."""
    try:
        head = lines.index(heading)
    except ValueError:
        raise UpgradeRowError(f"heading {heading!r} not found") from None
    start = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("|")), None)
    if start is None:
        raise UpgradeRowError(f"no table after heading {heading!r}")
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return start, end


def _data_rows(lines: list[str], heading: str) -> range:
    """Return the line indexes of the rows under the header and separator."""
    start, end = _table_block(lines, heading)
    return range(start + 2, end)


def check_table_text(text: str, heading: str, label: str) -> None:
    """Check one file: no conflict marker, 3 columns per row, no doubled row."""
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        if _MARKER_RE.match(line):
            raise UpgradeRowError(f"{label}:{number}: conflict marker line: {line!r}")
    start, end = _table_block(lines, heading)
    seen: set[str] = set()
    for number in range(start, end):
        line = lines[number]
        stripped = line.strip()
        if not stripped.endswith("|") or stripped.endswith("\\|"):
            raise UpgradeRowError(
                f"{label}:{number + 1}: the row must end with '|', "
                "else its columns cannot be counted"
            )
        columns = len(_PIPE_RE.split(line.strip())) - 2
        if columns != 3:
            raise UpgradeRowError(
                f"{label}:{number + 1}: the row has {columns} columns, expected 3"
            )
        if line in seen:
            raise UpgradeRowError(f"{label}:{number + 1}: the same row is there twice")
        seen.add(line)


def check_table_files(root: Path = REPO_ROOT) -> None:
    for rel, heading in TABLE_FILES.items():
        check_table_text((root / rel).read_text(encoding="utf-8"), heading, rel)


def released_versions(root: Path = REPO_ROOT) -> list[str]:
    """Return the released versions (``## [X.Y.Z]`` in CHANGELOG.md), newest first."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    return sorted((f"v{v}" for v in _RELEASED_RE.findall(text)), key=_version_key, reverse=True)


def check_frozen_tables(root: Path = REPO_ROOT, rows_dir: Path = ROWS_DIR) -> None:
    """Fail on a row added by hand below the frozen rows of a table."""
    released = set(released_versions(root))
    written = [row for row in collect_rows(rows_dir) if row.version in released]
    for rel, heading in TABLE_FILES.items():
        lines = (root / rel).read_text(encoding="utf-8").splitlines()
        rows = _data_rows(lines, heading)
        frozen = FROZEN_ROWS[rel]
        if len(rows) < frozen:
            raise UpgradeRowError(
                f"{rel}: the table has {len(rows)} rows, expected at least {frozen}; "
                "do not remove rows"
            )
        allowed = {row.as_table_line() for row in written} if rel == DEPLOYING else set()
        for index in rows[frozen:]:
            if lines[index] in allowed:
                continue
            if rel == DEPLOYING:
                _fail_on_changed_row(lines, index, written)
            raise UpgradeRowError(f"{rel}:{index + 1}: this row has no row file. {_MOVE_HINT}")


def _fail_on_changed_row(lines: list[str], index: int, written: list[Row]) -> None:
    """Fail when a written row of ``docs/deploying.md`` and its row file differ.

    The row has the version of row files whose own line is not in the table:
    one of the two was changed without the other.
    """
    table = set(lines)
    changed = [
        f"{row.version}/{row.name}.md"
        for row in written
        if lines[index].startswith(f"| **{row.version}** |") and row.as_table_line() not in table
    ]
    if changed:
        raise UpgradeRowError(
            f"{DEPLOYING}:{index + 1}: this row differs from {' or '.join(changed)}; "
            "change both in the same pull request"
        )


def add_rows_to_table(text: str, heading: str, rows: list[Row]) -> str:
    """Return ``text`` with ``rows`` added at the end of the table under ``heading``."""
    if not rows:
        return text
    lines = text.splitlines()
    _, end = _table_block(lines, heading)
    existing = set(lines)
    for row in rows:
        if row.as_table_line() in existing:
            raise UpgradeRowError(f"{row.version}/{row.name}.md: the table already holds this row")
    lines[end:end] = [row.as_table_line() for row in rows]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def missing_rows(text: str, heading: str, rows: list[Row]) -> list[Row]:
    """Return the rows that the table under ``heading`` does not hold yet."""
    lines = text.splitlines()
    start, end = _table_block(lines, heading)
    table = set(lines[start:end])
    return [row for row in rows if row.as_table_line() not in table]


def _missing_released_rows(root: Path, rows_dir: Path) -> list[Row]:
    """Return the rows of the newest release that ``docs/deploying.md`` lacks.

    Fails on a missing row that ``--write`` must not add: a row of an older
    release, or a row of the newest release when ``--write`` already ran for
    it (some of its rows are in the table). Such a row came after the release.
    """
    released = released_versions(root)
    if not released:
        return []
    rows = [row for row in collect_rows(rows_dir) if row.version in released]
    text = (root / DEPLOYING).read_text(encoding="utf-8")
    missing = missing_rows(text, TABLE_FILES[DEPLOYING], rows)
    newest = [row for row in rows if row.version == released[0]]
    written = any(row not in missing for row in newest)
    for row in missing:
        if row.version != released[0] or written:
            raise UpgradeRowError(
                f"{row.version}/{row.name}.md: {row.version} is already released; "
                "put the row in the folder of the next release"
            )
    return missing


def write_deploying(root: Path = REPO_ROOT, rows_dir: Path = ROWS_DIR) -> list[Row]:
    """Add the rows of the newest release that ``docs/deploying.md`` lacks; return them."""
    rows = _missing_released_rows(root, rows_dir)
    if rows:
        path = root / DEPLOYING
        text = path.read_text(encoding="utf-8")
        path.write_text(add_rows_to_table(text, TABLE_FILES[DEPLOYING], rows), encoding="utf-8")
    return rows


def check_released_rows(root: Path = REPO_ROOT, rows_dir: Path = ROWS_DIR) -> None:
    """Fail when a row of a released version is missing in ``docs/deploying.md``."""
    missing = _missing_released_rows(root, rows_dir)
    if missing:
        names = ", ".join(f"{row.version}/{row.name}.md" for row in missing)
        version = missing[0].version
        raise UpgradeRowError(
            f"{DEPLOYING} lacks rows of released versions ({names}). "
            f"In the release pull request of {version}, run "
            "python scripts/ci/upgrade_rows.py --write. "
            f"Else {version} is already released; put the row in the folder of the next release"
        )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise UpgradeRowError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def check_new_rows(base_ref: str, root: Path = REPO_ROOT) -> list[str]:
    """Fail on a row file added to the folder of a version released on the base.

    "Released" is read from ``CHANGELOG.md`` on the base ref, so the release
    pull request, which adds the ``## [X.Y.Z]`` heading itself, still passes.
    Returns the new row files that are in a folder that is not released.
    """
    base_changelog = _git(root, "show", f"{base_ref}:CHANGELOG.md")
    released = {f"v{v}" for v in _RELEASED_RE.findall(base_changelog)}
    diff = _git(
        root,
        "diff",
        "--diff-filter=AR",
        "--name-status",
        f"{base_ref}...HEAD",
        "--",
        "docs/upgrade-rows",
    )
    added: list[str] = []
    for line in diff.splitlines():
        path = line.split("\t")[-1]
        parts = path.split("/")
        if len(parts) != 4 or parts[3] == "README.md":
            continue
        if parts[2] in released:
            raise UpgradeRowError(
                f"{path}: {parts[2]} is released. Move the row to the folder of "
                f"the next release: git mv {path} docs/upgrade-rows/<next>/."
            )
        added.append(path)
    return added


def check_all(root: Path = REPO_ROOT, rows_dir: Path = ROWS_DIR) -> list[Row]:
    """Run every check; return the rows of the folder."""
    check_table_files(root)
    rows = collect_rows(rows_dir)
    check_frozen_tables(root, rows_dir)
    check_released_rows(root, rows_dir)
    return rows


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    """mkdocs hook: run every check and add the folder rows to the site page."""
    if page.file.src_uri != SITE_PAGE:
        return markdown
    try:
        rows = check_all(REPO_ROOT, ROWS_DIR)
        return add_rows_to_table(markdown, TABLE_FILES["docs-site/install/upgrades.md"], rows)
    except UpgradeRowError as err:
        from mkdocs.exceptions import PluginError

        raise PluginError(f"upgrade rows: {err}") from err


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the upgrade rows and tables.")
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"add the missing rows of the newest release to the table in {DEPLOYING}",
    )
    parser.add_argument(
        "--check-new-rows",
        metavar="BASE_REF",
        help="fail on a row file added to the folder of a version released on BASE_REF",
    )
    args = parser.parse_args(argv)
    try:
        if args.check_new_rows:
            added = check_new_rows(args.check_new_rows)
            print(f"new upgrade rows: {len(added)} added, none in a released folder")
            return 0
        if args.write:
            for row in write_deploying():
                print(f"added to {DEPLOYING}: {row.version}/{row.name}.md")
        rows = check_all()
    except UpgradeRowError as err:
        print(f"upgrade rows: {err}", file=sys.stderr)
        return 1
    for row in rows:
        print(row.as_table_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
