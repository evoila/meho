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
example ``v0.48.0``.

This file does four things:

* ``collect_rows`` reads and checks the row files.
* ``check_table_files`` checks the two tables (``docs/deploying.md`` and
  ``docs-site/install/upgrades.md``): no conflict marker line, 3 columns in
  every row, no doubled row.
* ``on_page_markdown`` is a ``mkdocs`` hook. It adds the rows of the folder
  to the end of the table of ``install/upgrades.md`` when the site is built.
  A broken row or table fails the build.
* ``write_deploying`` adds the rows of the folder that are not there yet to
  the end of the table in ``docs/deploying.md``. The release pull request
  runs it (``--write``), so contributors never edit that table.
  ``check_released_rows`` fails when a row of a released version (a
  ``## [X.Y.Z]`` heading in ``CHANGELOG.md``) is missing there.

``python scripts/ci/upgrade_rows.py`` runs the checks and prints the rows as
table lines, so you can see what the site will show.
``python scripts/ci/upgrade_rows.py --write`` also adds the missing rows to
``docs/deploying.md``.
"""

from __future__ import annotations

import argparse
import re
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
SITE_PAGE = "install/upgrades.md"

_VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_MARKER_RE = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")
_SECTIONS = ("Note", "Action")
_PIPE_RE = re.compile(r"(?<!\\)\|")
_RELEASED_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


class UpgradeRowError(Exception):
    """A row file or an upgrade table is wrong."""


@dataclass(frozen=True)
class Row:
    version: str
    name: str
    note: str
    action: str

    def sort_key(self) -> tuple[tuple[int, ...], str]:
        match = _VERSION_RE.match(self.version)
        assert match is not None
        return tuple(int(part) for part in match.groups()), self.name

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
        cells[name] = lines[0]
    return Row(version, path.stem, cells["Note"], cells["Action"])


def collect_rows(rows_dir: Path = ROWS_DIR) -> list[Row]:
    """Read every row file, check it, and return the rows in version order."""
    rows: list[Row] = []
    if not rows_dir.is_dir():
        return rows
    for version_dir in sorted(p for p in rows_dir.iterdir() if p.is_dir()):
        if not _VERSION_RE.match(version_dir.name):
            raise UpgradeRowError(
                f"{version_dir.name}: the folder name must be a version like v0.48.0"
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


def write_deploying(root: Path = REPO_ROOT, rows_dir: Path = ROWS_DIR) -> list[Row]:
    """Add the folder rows that ``docs/deploying.md`` lacks; return them."""
    path = root / DEPLOYING
    text = path.read_text(encoding="utf-8")
    heading = TABLE_FILES[DEPLOYING]
    rows = missing_rows(text, heading, collect_rows(rows_dir))
    if rows:
        path.write_text(add_rows_to_table(text, heading, rows), encoding="utf-8")
    return rows


def check_released_rows(root: Path = REPO_ROOT, rows_dir: Path = ROWS_DIR) -> None:
    """Fail when a row of a released version is missing in ``docs/deploying.md``."""
    released = {
        f"v{version}"
        for version in _RELEASED_RE.findall((root / "CHANGELOG.md").read_text(encoding="utf-8"))
    }
    rows = [row for row in collect_rows(rows_dir) if row.version in released]
    text = (root / DEPLOYING).read_text(encoding="utf-8")
    missing = missing_rows(text, TABLE_FILES[DEPLOYING], rows)
    if missing:
        names = ", ".join(f"{row.version}/{row.name}.md" for row in missing)
        raise UpgradeRowError(
            f"{DEPLOYING} lacks rows of released versions ({names}); "
            "run python scripts/ci/upgrade_rows.py --write"
        )


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    """mkdocs hook: check the tables and add the folder rows to the site page."""
    if page.file.src_uri != SITE_PAGE:
        return markdown
    try:
        check_table_files(REPO_ROOT)
        rows = collect_rows(ROWS_DIR)
        return add_rows_to_table(markdown, TABLE_FILES["docs-site/install/upgrades.md"], rows)
    except UpgradeRowError as err:
        from mkdocs.exceptions import PluginError

        raise PluginError(f"upgrade rows: {err}") from err


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the upgrade rows and tables.")
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"add the missing folder rows to the table in {DEPLOYING}",
    )
    args = parser.parse_args(argv)
    try:
        check_table_files()
        rows = collect_rows()
        if args.write:
            for row in write_deploying():
                print(f"added to {DEPLOYING}: {row.version}/{row.name}.md")
        check_released_rows()
    except UpgradeRowError as err:
        print(f"upgrade rows: {err}", file=sys.stderr)
        return 1
    for row in rows:
        print(row.as_table_line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
