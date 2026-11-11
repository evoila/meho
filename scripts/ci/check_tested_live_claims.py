#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when a text says "tested live" but the live test record has no such test.

The live test record, ``docs/compatibility/live-tests.yaml``, says which
connector versions were tested on a real system. The generated tables follow
the record. This check covers the text that people write by hand: any page
under ``docs-site/``, and the newest sections of ``CHANGELOG.md``.

A line that says "tested live" (or "live-tested", "live tested", "tested on a
real system") must name a connector id and a product version that have a
test in the record. The check skips the generated parts: the connector
inventory page and any block between ``<!-- live-tests:start -->`` and
``<!-- live-tests:end -->``.

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


def _has_token(text: str, token: str) -> bool:
    """True when *token* is in *text* and is not part of a longer version or word."""
    return re.search(rf"(?<![\w.]){re.escape(token)}(?![\w]|\.\d)", text) is not None


def line_is_recorded(line: str, tested: dict[str, set[str]]) -> bool:
    """True when *line* names a connector id and a version that the record has."""
    for connector_id, versions in tested.items():
        if connector_id not in line:
            continue
        rest = line.replace(connector_id, " ")
        if any(_has_token(rest, version) for version in versions):
            return True
    return False


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
