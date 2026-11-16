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
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

LEVELS = {"read", "change"}
TEST_KEYS = {"product_version", "level", "date", "meho_release"}
_RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)
# Digits and dots, with an optional suffix without dots, like "2.7.2-RELEASE".
# A suffix has no dot, so a host name or URL cannot hide in it.
_VERSION = re.compile(r"v?\d+(?:\.\d+){0,3}(?:[-+_][A-Za-z0-9]+)*")
# A connector id, like "vmware-rest-9.0" or "k8s-1.x". Its version part looks
# like a host name to _HOST, so a key that matches this is not checked for one.
_CONNECTOR_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-v?\d+(?:\.(?:\d+|x))*")
# A file name a comment may point to, like "CHANGELOG.md".
_FILE_REFERENCE = re.compile(r"[\w/-]+\.(?:md|py|yaml|yml)")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://|\bwww\.", re.IGNORECASE)
# A name with a dot whose last part has a letter, like "name.example.org".
# Version numbers (only digits and dots) do not match.
_HOST = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z0-9-]*\b")


_RELEASE_TAG = re.compile(r"refs/tags/v(\d+\.\d+\.\d+)$")


class TagLookupError(Exception):
    """The release tags could not be read."""


def release_tags(repo_root: Path) -> set[str]:
    """Return the ``X.Y.Z`` of every ``vX.Y.Z`` git tag on ``origin``.

    It reads only the tag names (``git ls-remote``). That works in a shallow
    checkout without tags and downloads no files. Raises ``TagLookupError``
    when git fails.
    """
    command = ["git", "ls-remote", "--tags", "--refs", "origin", "refs/tags/v*"]
    try:
        result = subprocess.run(
            command, cwd=repo_root, check=False, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TagLookupError(str(exc)) from exc
    if result.returncode != 0:
        reason = result.stderr.strip() or f"git exited with {result.returncode}"
        raise TagLookupError(reason)
    found = (_RELEASE_TAG.search(line) for line in result.stdout.splitlines())
    return {match.group(1) for match in found if match}


class _Releases:
    """The releases a row may name: changelog headings, then git tags.

    The tags are read once, and only when a release is not a heading.
    """

    def __init__(self, headings: set[str], tag_source: Callable[[], set[str]]) -> None:
        self._headings = headings
        self._tag_source = tag_source
        self._tags: set[str] | None = None
        self._error: str | None = None

    def problem(self, release: object) -> str | None:
        """Return why *release* is not accepted, or None when it is."""
        if isinstance(release, str) and release in self._headings:
            return None
        if not isinstance(release, str):
            return f'meho_release {release!r} must be a release like "0.45.2"'
        if self._tags is None and self._error is None:
            try:
                self._tags = self._tag_source()
            except TagLookupError as exc:
                self._error = str(exc)
        if self._error is not None:
            return (
                f"meho_release {release!r} is not a '## [X.Y.Z]' heading in CHANGELOG.md, "
                f"and the git tag v{release} could not be checked ({self._error})"
            )
        if self._tags is not None and release in self._tags:
            return None
        return (
            f"meho_release {release!r} is not a '## [X.Y.Z]' heading in CHANGELOG.md "
            f"and has no git tag v{release}"
        )


def changelog_releases(changelog_text: str) -> set[str]:
    """Return the ``X.Y.Z`` of every ``## [X.Y.Z]`` heading."""
    return set(_RELEASE_HEADING.findall(changelog_text))


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]  # yaml has no stubs here
    """A safe loader that refuses a key listed twice in one mapping.

    ``yaml.safe_load`` keeps only the last of two equal keys, so a second block
    for the same connector would replace the first one without a word.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Any:
        seen: set[object] = set()
        for key_node, _value in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"key {key!r} is listed twice; keep one", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_record(raw_text: str) -> Any:
    """Parse the record. Raises ``yaml.YAMLError`` on bad YAML or a repeated key."""
    # A SafeLoader subclass, so it makes no Python objects.
    return yaml.load(raw_text, Loader=_UniqueKeyLoader)


def _lab_detail(text: str, *, allowed: re.Pattern[str] | None = None) -> str | None:
    """Name the first URL, address or host name in *text*.

    A host name that fully matches *allowed* does not count.
    """
    checks = (("URL", _URL), ("IPv4 address", _IPV4), ("host name", _HOST))
    for kind, pattern in checks:
        for match in pattern.finditer(text):
            if kind == "host name" and allowed and allowed.fullmatch(match.group(0)):
                continue
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
    label: str, test: object, releases: _Releases, today: dt.date, seen: set[str]
) -> list[str]:
    if not isinstance(test, dict):
        return [f"{label}: a test must be a mapping"]
    problems = [f"{label}: unknown field {key!r}" for key in sorted(set(test) - TEST_KEYS)]
    version = test.get("product_version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        problems.append(
            f'{label}: product_version must be quoted text like "8.0.3" or "2.7.2-RELEASE"'
        )
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
    release_problem = releases.problem(test.get("meho_release"))
    if release_problem:
        problems.append(f"{label}: {release_problem}")
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
    connector_id: object, entry: object, releases: _Releases, today: dt.date
) -> list[str]:
    label = f"connector {connector_id}"
    problems: list[str] = []
    if not isinstance(connector_id, str) or not _CONNECTOR_ID.fullmatch(connector_id):
        problems.append(f'{label}: the id must look like "vmware-rest-9.0" or "k8s-1.x"')
    if not isinstance(entry, dict):
        return [*problems, f"{label}: must be a mapping"]
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
        # A connector id like "k8s-1.x" looks like a host name; its form is checked elsewhere.
        is_id = path.startswith("file.connectors.<key ")
        detail = _lab_detail(text, allowed=_CONNECTOR_ID if is_id else None)
        if detail:
            problems.append(f"{path}: lab detail not allowed ({detail})")
    for number, line in enumerate(raw_text.splitlines(), start=1):
        if "#" in line:
            detail = _lab_detail(line.split("#", 1)[1], allowed=_FILE_REFERENCE)
            if detail:
                problems.append(f"line {number}: lab detail not allowed in a comment ({detail})")
    return problems


def validate_live_tests(
    data: object,
    raw_text: str,
    changelog_text: str,
    today: dt.date,
    tag_source: Callable[[], set[str]] | None = None,
) -> list[str]:
    """Return one message for each problem. An empty list means the file is good.

    A release is good when it is a changelog heading or a git tag. *tag_source*
    gives the tag set; tests pass a fixed one. The default asks ``origin``.
    """
    if not isinstance(data, dict):
        return ["the file must be a mapping"]
    problems = _check_header(data)
    connectors = data.get("connectors")
    if not isinstance(connectors, dict):
        return [*problems, "connectors must be a mapping"]
    if tag_source is None:
        repo_root = Path(__file__).resolve().parents[2]

        def tag_source() -> set[str]:
            return release_tags(repo_root)

    releases = _Releases(changelog_releases(changelog_text), tag_source)
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
        data = load_record(raw)
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
