#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when a text says "tested live" but the live test record has no such test.

The live test record, ``docs/compatibility/live-tests.yaml``, says which
connector versions were tested on a real system. The generated tables follow
the record. This check covers the text that people write by hand: any page
under ``docs-site/``, and the newest sections of ``CHANGELOG.md``.

A line that says "tested live" (or "live-tested", "live tested", "tested on a
real system") must name a connector id and a product version. Each version
must have a test in the record for the connector id before it. Ids and
versions match only in full: ``4.2.1`` does not back ``4.2.1-rc1``, and
``nsx-rest-4.2`` does not back ``nsx-rest-4.20``. A leading ``v`` does not
count: ``v4.2.1`` and ``4.2.1`` are the same version. Every word shaped like a
connector id (such as ``nsx-typed-4.2``) counts, also when the record does not
know it. Every number on the line counts as a version, so write other numbers
in words. A version is read as a whole word: a word that starts with a number
but has no version shape the record accepts (such as ``4.2.1+build.5`` or
``4.2.2-rc.1``) fails the line. The check skips the generated parts: the
connector inventory page and any block between ``<!-- live-tests:start -->``
and ``<!-- live-tests:end -->``.

It also runs ``backend/scripts/validate_live_tests.py`` on the record.

It uses only the standard library and PyYAML, so the docs CI job can run it
with no backend install.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_CLAIM = re.compile(r"tested[ -]live|live[ -]tested|tested on a real system", re.IGNORECASE)
_START = "<!-- live-tests:start -->"
_END = "<!-- live-tests:end -->"
_GENERATED_PAGES = {"docs-site/reference/connectors.md"}
_SECTION = re.compile(r"^## \[", re.MULTILINE)
_FIX = "add a row to docs/compatibility/live-tests.yaml or change the text"
_RECORD = "docs/compatibility/live-tests.yaml"
# The version and connector id shapes of backend/scripts/validate_live_tests.py,
# so every version the record accepts (2022, 4.2.1_rc1, v4.2.1) can be claimed.
# A connector id here starts with a letter, so a date is no connector id.
VERSION_SHAPE = r"v?\d+(?:\.\d+){0,3}(?:[-+_][A-Za-z0-9]+)*"
CONNECTOR_ID_SHAPE = r"[a-z0-9]+(?:-[a-z0-9]+)*-v?\d+(?:\.(?:\d+|x))*"
# Only complete words: "4.2" in "4.2.1" or "nsx-rest-4.2" in "nsx-rest-4.20" is none.
_BEFORE = r"(?<![\w.-])"
_AFTER = r"(?![\w-]|\.\w)"
_VERSION = re.compile(VERSION_SHAPE)
# A whole word that starts with a number. The end of a sentence ("4.2.1.") is
# not part of it.
_NUMBER_WORD = re.compile(r"(?<![\w.+-])v?\d[\w.+-]*")
_WORD_END = ".+-"
_CONNECTOR_ID = re.compile(_BEFORE + r"(?=[a-z])" + CONNECTOR_ID_SHAPE + _AFTER)
# A list marker at the start of a line, like "- " or "1. ", is no version.
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def recorded_tests(record: Any) -> dict[str, set[str]]:
    """Return ``{connector id: {product versions}}`` for connectors with a test."""
    tested: dict[str, set[str]] = {}
    connectors = record.get("connectors") if isinstance(record, dict) else None
    for connector_id, entry in (connectors or {}).items():
        tests = entry.get("tests") if isinstance(entry, dict) else None
        versions = {
            str(test["product_version"])
            for test in tests or []
            if isinstance(test, dict) and "product_version" in test
        }
        if versions:
            tested[str(connector_id)] = versions
    return tested


def _connector_spans(line: str, connector_ids: set[str]) -> list[tuple[int, int, str]]:
    """Return ``(start, end, id)`` for each complete connector id in *line*.

    An id counts when the record knows it or when it has the shape of an id, so
    an id with no row in the record is not missed. ``nsx-rest-4.2`` does not
    match inside ``nsx-rest-4.20`` or ``my-nsx-rest-4.2``.
    """
    found = {(m.start(), m.end(), m.group()) for m in _CONNECTOR_ID.finditer(line)}
    for connector_id in connector_ids:
        pattern = _BEFORE + re.escape(connector_id) + _AFTER
        found.update((m.start(), m.end(), connector_id) for m in re.finditer(pattern, line))
    # Keep an id only when no longer id holds it.
    return sorted(
        span
        for span in found
        if not any(o != span and o[0] <= span[0] and span[1] <= o[1] for o in found)
    )


def claimed_pairs(line: str, connector_ids: set[str]) -> list[tuple[str, str]] | None:
    """Return each ``(connector id, version)`` that *line* claims, or None.

    A version belongs to the nearest connector id before it, or to the first id
    when no id comes before it. None means the line names no known connector,
    names no version, or names a connector with no version.
    """
    line = _LIST_MARKER.sub(lambda m: " " * len(m.group()), line)
    spans = _connector_spans(line, connector_ids)
    if not spans:
        return None
    pairs: list[tuple[str, str]] = []
    # The text before the first id, between two ids, and after the last id.
    # The ids themselves are left out, so "4.2" in "nsx-rest-4.2" is no version.
    owners = [spans[0][2]] + [connector_id for _, _, connector_id in spans]
    starts = [start for start, _, _ in spans] + [len(line)]
    ends = [0] + [end for _, end, _ in spans]
    for owner, start, end in zip(owners, ends, starts, strict=True):
        versions = [m.group().rstrip(_WORD_END) for m in _NUMBER_WORD.finditer(line[start:end])]
        if not all(_VERSION.fullmatch(version) for version in versions):
            return None
        pairs.extend((owner, version) for version in versions)
    named = {connector_id for _, _, connector_id in spans}
    if named - {connector_id for connector_id, _ in pairs}:
        return None
    return pairs


def _without_v(version: str) -> str:
    return version.removeprefix("v")


def line_is_recorded(line: str, tested: dict[str, set[str]]) -> bool:
    """True when every connector version that *line* claims has a test in the record."""
    pairs = claimed_pairs(line, set(tested))
    return pairs is not None and all(
        _without_v(version) in {_without_v(v) for v in tested.get(c, set())} for c, version in pairs
    )


def claim_lines(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, line)`` for each claim outside a generated block."""
    found: list[tuple[int, str]] = []
    generated = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _START in line:
            generated = True
        elif _END in line:
            generated = False
        elif not generated and _CLAIM.search(line):
            found.append((number, line))
    return found


def newest_changelog_sections(text: str) -> tuple[str, int]:
    """Return the text of ``[Unreleased]`` and the newest release, and its line offset.

    The text starts at the first ``## [`` heading and ends before the third.
    The offset keeps line numbers right. A changelog with no heading gives no text.
    """
    starts = [match.start() for match in _SECTION.finditer(text)]
    if not starts:
        return "", 0
    end = starts[2] if len(starts) > 2 else len(text)
    return text[starts[0] : end], text.count("\n", 0, starts[0])


def scan(root: Path, tested: dict[str, set[str]]) -> list[str]:
    """Return one problem text for each claim that the record does not back."""
    problems: list[str] = []

    def check(path: Path, text: str, offset: int = 0) -> None:
        for number, line in claim_lines(text):
            if not line_is_recorded(line, tested):
                where = f"{path.relative_to(root).as_posix()}:{number + offset}"
                problems.append(
                    f"{where}: says the version was tested live, but the record has no "
                    f"such test. Line: {line.strip()}\n  Fix: {_FIX}."
                )

    for page in sorted((root / "docs-site").rglob("*.md")):
        if page.relative_to(root).as_posix() not in _GENERATED_PAGES:
            check(page, page.read_text(encoding="utf-8"))
    changelog = root / "CHANGELOG.md"
    if changelog.is_file():
        section, offset = newest_changelog_sections(changelog.read_text(encoding="utf-8"))
        check(changelog, section, offset)
    return problems


def run_record_check(root: Path) -> int:
    """Run the record check of ``backend/scripts/validate_live_tests.py``."""
    script = Path(__file__).resolve().parents[2] / "backend" / "scripts" / "validate_live_tests.py"
    command = [
        sys.executable,
        str(script),
        "--file",
        str(root / _RECORD),
        "--changelog",
        str(root / "CHANGELOG.md"),
    ]
    return subprocess.run(command, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    root: Path = args.root
    try:
        record = yaml.safe_load((root / _RECORD).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"tested live claims: cannot read {_RECORD}: {exc}", file=sys.stderr)
        return 1
    problems = scan(root, recorded_tests(record))
    for problem in problems:
        print(problem, file=sys.stderr)
    status = run_record_check(root)
    if problems or status:
        return 1
    print("tested live claims passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
