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
  removed, or its inputs changed in a way that can break a package (a new
  input that can be given by its place, a new input with no default, a
  changed default, type or meaning of an input that exists, a new input on
  a function with a ``**`` input, or any other change of the name, for
  example its answer type); at least the second number when names were only
  added, marked as old, or a function only got new inputs that are given
  only by name (after the ``*`` or after a ``*args`` input) and have a
  default. A function with no such input before may get the ``*``
  together with the new inputs;
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
import ast
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
_INPUT_NAME = re.compile(r"\*{0,2}([A-Za-z_]\w*)")


class InputError(Exception):
    """A file or git read failed; the script exits 2."""


def _version_tuple(version: str) -> tuple[int, int]:
    first, second = (int(part) for part in version.split("."))
    return first, second


def _without_old(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if key != "old"}


def _has_default(text: str) -> bool:
    """True when the input text (``name: type = default``) has a default.

    The default starts after the first `` = `` whose text before it is a whole
    input that Python can parse. A `` = `` inside a type, for example
    ``Literal['a = b']``, leaves a text before it that Python cannot parse.
    """
    start = text.find(" = ")
    while start != -1:
        try:
            ast.parse(f"def _({text[:start]}): pass")
        except SyntaxError:
            start = text.find(" = ", start + 1)
        else:
            return True
    return False


def _parsed_inputs(inputs: list[str]) -> list[tuple[str, str, str]]:
    """Each input as (name, text, how a call gives it), without the ``/`` and ``*`` marks.

    How a call gives it: ``place`` (only by place), ``either``, ``*args``,
    ``name`` (only by name: after a ``*`` or a ``*args`` input) or ``**``.
    """
    parsed: list[tuple[str, str, str]] = []
    how = "place" if "/" in inputs else "either"
    for text in inputs:
        if text in ("/", "*"):
            how = "either" if text == "/" else "name"
            continue
        match = _INPUT_NAME.match(text)
        name = match.group(1) if match else text
        if text.startswith("**"):
            parsed.append((name, text, "**"))
        elif text.startswith("*"):
            parsed.append((name, text, "*args"))
            how = "name"
        else:
            parsed.append((name, text, how))
    return parsed


def _new_optional_named_inputs(old: dict[str, Any], new: dict[str, Any]) -> list[str] | None:
    """The new inputs when *new* is *old* plus inputs given only by name, each with a default.

    ``None`` when the change can break a package: a new input given by its
    place, a new input with no default, a changed input that exists, a new
    input on a function with a ``**`` input, or any other change of the
    entry. A function without a ``*`` before may get the ``*`` together with
    the new inputs.
    """
    if {k: v for k, v in old.items() if k != "inputs"} != {
        k: v for k, v in new.items() if k != "inputs"
    }:
        return None
    old_inputs, new_inputs = old.get("inputs"), new.get("inputs")
    if old_inputs is None or new_inputs is None:
        return None
    old_parsed, new_parsed = _parsed_inputs(old_inputs), _parsed_inputs(new_inputs)
    old_names = {name for name, _, _ in old_parsed}
    kept = [item for item in new_parsed if item[0] in old_names]
    added = [item for item in new_parsed if item[0] not in old_names]
    if not added or kept != old_parsed or any(how == "**" for _, _, how in old_parsed):
        return None
    if any(how != "name" or not _has_default(text) for _, text, how in added):
        return None
    return [text for _, text, _ in added]


def _only_optional_named_inputs_added(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """True when *new* is *old* plus inputs given only by name, each with a default.

    Such an input breaks no package: a call that leaves it out works as
    before. So it raises only the second number of the kit version. The
    same rule as ``only_optional_named_inputs_added`` in
    ``backend/scripts/write_connector_kit_names.py``.
    """
    return _new_optional_named_inputs(old, new) is not None


def kit_problems(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """Problems with the kit version and the removed names. Empty when fine."""
    if old is None:
        return []
    old_names: dict[str, dict[str, Any]] = old["names"]
    new_names: dict[str, dict[str, Any]] = new["names"]
    removed = sorted(set(old_names) - set(new_names))
    added = sorted(set(new_names) - set(old_names))
    common = sorted(set(old_names) & set(new_names))
    differ = [n for n in common if _without_old(old_names[n]) != _without_old(new_names[n])]
    extended = [
        n
        for n in differ
        if _only_optional_named_inputs_added(_without_old(old_names[n]), _without_old(new_names[n]))
    ]
    changed = [n for n in differ if n not in extended]
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
    elif (added or extended or marked) and new_version <= old_version:
        problems.append(
            "Kit names were added, got new optional inputs, or were marked as old "
            f"({', '.join(dict.fromkeys(added + extended + marked))}), so the "
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
