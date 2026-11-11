#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the live test record, ``docs/compatibility/live-tests.yaml``.

The record says which connector versions were tested on a real system. This
check keeps the file correct and free of lab details. It prints one line for
each problem and exits non-zero when there is any.

It needs only the standard library and PyYAML, so the docs CI job can run it.
Whether each connector id exists is checked by a unit test, because that needs
the connector registry (``backend/tests/test_live_tests_record.py``).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import yaml

LEVELS = {"read", "change"}
TEST_KEYS = {"product_version", "level", "date", "meho_release"}
_RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
_VERSION = re.compile(r"\d+(?:\.\d+){0,3}")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|\bwww\.", re.IGNORECASE)
# A name with a dot whose last part has a letter, like "name.example.org".
# Version numbers (only digits and dots) do not match.
_HOST = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z0-9-]*\b")


def changelog_releases(changelog_text: str) -> set[str]:
    """Return the ``X.Y.Z`` of every ``## [X.Y.Z]`` heading."""
    return set(_RELEASE_HEADING.findall(changelog_text))


def _lab_detail(text: str, *, comment: bool = False) -> str | None:
    # A comment may name a file, like "CHANGELOG.md", so it is not checked for host names.
    checks = (("URL", _URL), ("IPv4 address", _IPV4), ("host name", _HOST))
    for kind, pattern in checks[:2] if comment else checks:
        match = pattern.search(text)
        if match:
            return f"{kind} {match.group(0)!r}"
    return None


def _strings(node: object, path: str) -> list[tuple[str, str]]:
    """Every text in the file with its path, except the ``product_version`` values."""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((f"{path}.<key {key}>", str(key)))
            if key != "product_version":
                found += _strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += _strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        found.append((path, node))
    return found


def _check_test(
    label: str, test: object, releases: set[str], today: dt.date, seen: set[str]
) -> list[str]:
    if not isinstance(test, dict):
        return [f"{label}: a test must be a mapping"]
    problems = [f"{label}: unknown field {key!r}" for key in sorted(set(test) - TEST_KEYS)]
    version = test.get("product_version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        problems.append(f'{label}: product_version must be quoted text like "8.0.3"')
    elif version in seen:
        problems.append(f"{label}: product_version {version} is listed twice; keep one entry")
    else:
        seen.add(version)
    if test.get("level") not in LEVELS:
        problems.append(f"{label}: level must be one of {sorted(LEVELS)}")
    date = test.get("date")
    if isinstance(date, dt.datetime) or not isinstance(date, dt.date):
        problems.append(f"{label}: date must be an ISO date like 2026-11-07")
    elif date > today:
        problems.append(f"{label}: date {date} is in the future")
    release = test.get("meho_release")
    if not isinstance(release, str) or release not in releases:
        problems.append(
            f"{label}: meho_release {release!r} is not a '## [X.Y.Z]' heading in CHANGELOG.md"
        )
    return problems


def _check_header(data: dict[object, object]) -> list[str]:
    problems: list[str] = []
    if data.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    levels = data.get("levels")
    if not isinstance(levels, dict) or set(levels) != LEVELS:
        problems.append(f"levels must describe exactly {sorted(LEVELS)}")
    stale = data.get("stale_after_minor_releases")
    if isinstance(stale, bool) or not isinstance(stale, int) or stale < 1:
        problems.append("stale_after_minor_releases must be a positive integer")
    return problems


def _check_connector(
    connector_id: object, entry: object, releases: set[str], today: dt.date
) -> list[str]:
    label = f"connector {connector_id}"
    if not isinstance(entry, dict):
        return [f"{label}: must be a mapping"]
    problems: list[str] = []
    product = entry.get("product")
    if not isinstance(product, str) or not product.strip():
        problems.append(f"{label}: product must be non-empty text")
    tests = entry.get("tests")
    if not isinstance(tests, list):
        return [*problems, f"{label}: tests must be a list (use [] when not tested live)"]
    seen: set[str] = set()
    for index, test in enumerate(tests):
        problems += _check_test(f"{label}, test {index + 1}", test, releases, today, seen)
    return problems


def _check_lab_details(data: object, raw_text: str) -> list[str]:
    problems: list[str] = []
    for path, text in _strings(data, "file"):
        detail = _lab_detail(text)
        if detail:
            problems.append(f"{path}: lab detail not allowed ({detail})")
    for number, line in enumerate(raw_text.splitlines(), start=1):
        if "#" in line:
            detail = _lab_detail(line.split("#", 1)[1], comment=True)
            if detail:
                problems.append(f"line {number}: lab detail not allowed in a comment ({detail})")
    return problems


def validate_live_tests(
    data: object, raw_text: str, changelog_text: str, today: dt.date
) -> list[str]:
    """Return one message for each problem. An empty list means the file is good."""
    if not isinstance(data, dict):
        return ["the file must be a mapping"]
    problems = _check_header(data)
    connectors = data.get("connectors")
    if not isinstance(connectors, dict):
        return [*problems, "connectors must be a mapping"]
    releases = changelog_releases(changelog_text)
    for connector_id, entry in connectors.items():
        problems += _check_connector(connector_id, entry, releases, today)
    return problems + _check_lab_details(data, raw_text)


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=root / "docs/compatibility/live-tests.yaml")
    parser.add_argument("--changelog", type=Path, default=root / "CHANGELOG.md")
    args = parser.parse_args()
    try:
        raw = args.file.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        changelog = args.changelog.read_text(encoding="utf-8")
    except (OSError, yaml.YAMLError) as exc:
        print(f"live test record: cannot read: {exc}", file=sys.stderr)
        return 1
    problems = validate_live_tests(data, raw, changelog, dt.datetime.now(dt.UTC).date())
    for problem in problems:
        print(f"live test record: {problem}", file=sys.stderr)
    if problems:
        return 1
    count = sum(len(entry["tests"]) for entry in data["connectors"].values())
    print(f"live test record passed: connectors={len(data['connectors'])}, tests={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
