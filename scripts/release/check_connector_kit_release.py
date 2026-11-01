#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the connector kit part of a release (step 2 of docs/RELEASING.md).

The connector kit is the module ``meho_connector_kit``: the MEHO names a
connector package may import. ``backend/connector-kit/kit-names.json`` lists
them with their inputs, and holds the kit version. A test keeps the file in
step with the code, and ``backend/scripts/write_connector_kit_names.py``
raises the kit version on each change.

This script compares the file at the last release tag with the file now.
When the kit changed, it checks:

* the kit version went up far enough: the first number when a name was
  removed or changed, at least the second number when names were only
  added or marked as old;
* every removed name was marked as old at the last release tag;
* the release's section of ``CHANGELOG.md`` has a ``### Connector kit``
  section that names the new kit version.

When the kit did not change, it prints that and exits 0.

Exit codes: ``0`` all good; ``1`` a check failed (each problem is listed);
``2`` an argument, file or git error.

Usage, from the repository root, after the CHANGELOG roll::

    python3 scripts/release/check_connector_kit_release.py --version X.Y.Z

``--since`` names the last release tag (default: ``git describe --tags
--abbrev=0 HEAD``). ``--old-names`` reads the old list from a file instead of
git, and ``--new-names`` and ``--changelog`` point at other files; the tests
use them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
NAMES_PATH = "backend/connector-kit/kit-names.json"
SECTION = "### Connector kit"
COMMAND = "cd backend && uv run python -m scripts.write_connector_kit_names"


class InputError(Exception):
    """A file or git read failed; the script exits 2."""


def _version_tuple(version: str) -> tuple[int, int]:
    first, second = (int(part) for part in version.split("."))
    return first, second


def _without_old(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "old"}


def kit_problems(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Problems with the kit version and the removed names. Empty when fine."""
    if old is None:
        return []
    old_names: dict[str, dict[str, Any]] = old["names"]
    new_names: dict[str, dict[str, Any]] = new["names"]
    removed = sorted(set(old_names) - set(new_names))
    added = sorted(set(new_names) - set(old_names))
    common = sorted(set(old_names) & set(new_names))
    changed = [n for n in common if _without_old(old_names[n]) != _without_old(new_names[n])]
    marked = [n for n in common if old_names[n].get("old") != new_names[n].get("old")]
    problems: list[str] = []
    refused = [name for name in removed if "old" not in old_names[name]]
    if refused:
        problems.append(
            "These kit names were removed, but the last release did not mark them as "
            f"old: {', '.join(refused)}. An old name must stay for one kit version."
        )
    old_version = _version_tuple(old["kit_version"])
    new_version = _version_tuple(new["kit_version"])
    if removed or changed:
        if new_version[0] <= old_version[0]:
            problems.append(
                f"Kit names were removed or changed ({', '.join(removed + changed)}), so the "
                f"first number of the kit version must go up. It is {new['kit_version']}, and "
                f"it was {old['kit_version']} at the last release. Run `{COMMAND}`."
            )
    elif (added or marked) and new_version <= old_version:
        problems.append(
            f"Kit names were added or marked as old ({', '.join(added + marked)}), so the "
            f"kit version must go up. It is {new['kit_version']}, as at the last release. "
            f"Run `{COMMAND}`."
        )
    return problems


def release_section(changelog: str, version: str) -> str | None:
    """The text of ``## [<version>]`` in the CHANGELOG, or ``None``."""
    lines = changelog.splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    for start, line in enumerate(lines):
        if heading.match(line):
            end = next(
                (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
                len(lines),
            )
            return "\n".join(lines[start + 1 : end])
    return None


def notes_problems(section: str | None, version: str, kit_version: str) -> list[str]:
    """Problems with the ``### Connector kit`` section. Empty when fine."""
    if section is None:
        return [f"CHANGELOG.md has no `## [{version}]` section. Roll the CHANGELOG first."]
    lines = section.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == SECTION]
    if not starts:
        return [
            f"The kit changed, so the `## [{version}]` section needs a `{SECTION}` "
            f"section that names kit version {kit_version}."
        ]
    start = starts[0]
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("#")),
        len(lines),
    )
    body = "\n".join(lines[start + 1 : end])
    if not re.search(rf"(?<![\d.]){re.escape(kit_version)}(?![\d]|\.\d)", body):
        return [f"The `{SECTION}` section does not name the new kit version {kit_version}."]
    return []


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    return data


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )


def _old_names_at(tag: str | None) -> tuple[str, dict[str, Any] | None]:
    if tag is None:
        described = _git("describe", "--tags", "--abbrev=0", "HEAD")
        if described.returncode != 0:
            raise InputError(f"cannot find the last release tag: {described.stderr.strip()}")
        tag = described.stdout.strip()
    if _git("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}").returncode != 0:
        raise InputError(f"{tag} is not a tag or commit in this repository")
    shown = _git("show", f"{tag}:{NAMES_PATH}")
    if shown.returncode != 0:
        # The kit did not exist at that release: everything in it is new.
        return tag, None
    try:
        data: dict[str, Any] = json.loads(shown.stdout)
    except ValueError as exc:
        raise InputError(f"cannot read {NAMES_PATH} at {tag}: {exc}") from exc
    return tag, data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="the MEHO release, e.g. 0.42.0")
    parser.add_argument("--since", help="the last release tag (default: git describe)")
    parser.add_argument("--changelog", type=Path, default=REPO_ROOT / "CHANGELOG.md")
    parser.add_argument("--new-names", type=Path, default=REPO_ROOT / NAMES_PATH)
    parser.add_argument("--old-names", type=Path, help="read the old list from this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    old: dict[str, Any] | None
    try:
        new = _load_json(args.new_names)
        if args.old_names is not None:
            since, old = str(args.old_names), _load_json(args.old_names)
        else:
            since, old = _old_names_at(args.since)
        changelog = args.changelog.read_text(encoding="utf-8")
    except (InputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if old == new:
        print(f"connector kit: unchanged since {since} (kit version {new['kit_version']}).")
        return 0

    problems = kit_problems(old, new)
    section = release_section(changelog, args.version)
    problems += notes_problems(section, args.version, new["kit_version"])
    if problems:
        print(f"connector kit: {len(problems)} problem(s) since {since}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"connector kit: changed since {since}; kit version {new['kit_version']} is noted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
