# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail a CI shard when a test file skips more tests than allowed.

A skipped test shows as green in CI. So a test that stops running (a
missing tool, a missing setting) stays hidden. This tool reads the JUnit
file of one shard and counts the skipped tests per test file. Each count
must not be above the number for that file in ``tests/_skip_limits.json``.

- The count file holds one row per test file and lane (``unit`` or
  ``integration``). A test file that is not in it may skip 0 tests.
- A row is one number, or two numbers for the two kinds of run:
  ``{"with_secrets": 0, "without_secrets": 4}``. A run without the
  repository secrets (``--without-secrets``) has no vendor API files
  (unit lane) and no Keycloak test image (integration lane), so the tests
  that need them skip. Such a run takes the second number. Every other
  run takes the first one.
- A row names a test file in the plain form ``tests/<path>.py``: no
  absolute path, no ``.`` or ``..`` part.
- A pull request that adds a skip raises the number in the same pull
  request, so the raise is visible in the diff.
- A file that ran whole and skipped fewer tests than its number gives a
  notice ("lower the number"), not a failure. Some integration skips
  depend on how fast pods start, and a lucky run must not fail a pull
  request. A file that the run picked by test id (``--selection``) gives
  no such notice: the tests that skip may not have run.
- Expected failures (xfail) count as skips: JUnit writes both as
  ``<skipped>``.

The count is per test file, not per shard: the shard split moves files
between shards, but a file always runs whole in one shard. So each shard
checks its own JUnit file, and a short run (one connector, or the tests
picked by the test map) passes when no file is above its number.

The tool fails on the safe side: a missing, empty or broken JUnit file, a
missing or broken count file, an unknown lane, a row name that is not in
the plain form, or a row for a test file that is not in the repo all fail
the check.

CI usage (see ``python-unit-shard`` and ``python-integration-shard`` in
``.github/workflows/ci.yml``), from ``backend/``::

    uv run python tests/_skip_limit_check.py --lane unit --selection FILES [--without-secrets] JUNIT

This is CI tooling, not a test (the leading underscore keeps pytest from
collecting it).
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIMITS_FILE = Path(__file__).with_name("_skip_limits.json")
LANES = ("unit", "integration")
RUN_KINDS = ("with_secrets", "without_secrets")


class CheckError(Exception):
    """The check cannot run: an input is missing or cannot be read."""


@dataclass
class Result:
    """What the check found for one JUnit file."""

    errors: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _count(value: object) -> int | None:
    """Return ``value`` when it is a whole number of 0 or more, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _name_problem(name: str, backend_dir: Path) -> str | None:
    parts = name.split("/")
    if (
        name.startswith("/")
        or parts[0] != "tests"
        or not name.endswith(".py")
        or any(part in ("", ".", "..") for part in parts)
    ):
        return "is not a test file in the plain form tests/<path>.py"
    if not (backend_dir / name).is_file():
        return "is not in the repo; remove this row"
    return None


def _row_number(value: object, without_secrets: bool) -> int | str:
    """Return the number of one row for this kind of run, or the problem.

    Both numbers of a two-number row must be valid, whatever the run.
    """
    if isinstance(value, dict):
        if set(value) != set(RUN_KINDS):
            return f"needs exactly the keys {' and '.join(RUN_KINDS)}"
        numbers = {kind: _count(value[kind]) for kind in RUN_KINDS}
        number = numbers[RUN_KINDS[1] if without_secrets else RUN_KINDS[0]]
        if any(n is None for n in numbers.values()):
            number = None
    else:
        number = _count(value)
    return "needs a whole number of 0 or more" if number is None else number


def load_limits(
    path: Path, lane: str, without_secrets: bool = False, backend_dir: Path = BACKEND_DIR
) -> dict[str, int]:
    """Return the allowed skips per test file for ``lane`` and this kind of run.

    Raises ``CheckError`` when the file is missing or broken, names an
    unknown lane, holds a number that is not a whole number of 0 or more,
    or has a row name that is not a plain ``tests/<path>.py`` test file in
    the repo.
    """
    if lane not in LANES:
        raise CheckError(f"unknown lane {lane!r}; use one of {', '.join(LANES)}")
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CheckError(f"cannot read the count file {path}: {exc}") from exc
    lanes = document.get("lanes") if isinstance(document, dict) else None
    if not isinstance(lanes, dict):
        raise CheckError(f"{path}: the count file needs a 'lanes' object")
    unknown = sorted(set(lanes) - set(LANES))
    if unknown:
        raise CheckError(f"{path}: unknown lane(s) {', '.join(unknown)}; use {', '.join(LANES)}")
    problems: list[str] = []
    limits: dict[str, int] = {}
    for lane_name, rows in lanes.items():
        if not isinstance(rows, dict):
            raise CheckError(f"{path}: lane {lane_name!r} must be an object")
        for name, value in rows.items():
            number = _row_number(value, without_secrets)
            problem = number if isinstance(number, str) else _name_problem(name, backend_dir)
            if problem:
                problems.append(f"{lane_name}: {name} {problem}")
            elif lane_name == lane and isinstance(number, int):
                limits[name] = number
    if problems:
        raise CheckError(f"{path}: " + "; ".join(problems))
    return limits


def files_picked_by_test_id(selection: Path) -> set[str]:
    """Return the test files that the run picked by test id, not whole.

    ``selection`` holds what pytest was asked to run, one entry per line:
    a test file (``tests/test_x.py``) or a test id
    (``tests/test_x.py::test_one``). A file that is also named whole ran
    whole. Raises ``CheckError`` when the file cannot be read.
    """
    try:
        entries = [line.strip() for line in selection.read_text().splitlines()]
    except OSError as exc:
        raise CheckError(f"cannot read the selection file {selection}: {exc}") from exc
    whole = {entry for entry in entries if entry and "::" not in entry}
    by_id = {entry.split("::", 1)[0] for entry in entries if "::" in entry}
    return by_id - whole


def count_skips(junit_xml: Path) -> Counter[str]:
    """Return the number of skipped tests per test file in one JUnit file.

    Every test file of the JUnit file is in the result, also with 0 skips,
    so the check knows which files ran in this shard. Needs the xunit1
    family (``-o junit_family=xunit1``), which writes the test file name on
    every test case. Raises ``CheckError`` when the file
    is missing, empty, broken or holds no test case.
    """
    try:
        root = ET.parse(junit_xml).getroot()
    except (OSError, ET.ParseError) as exc:
        raise CheckError(f"cannot read the JUnit file {junit_xml}: {exc}") from exc
    skips: Counter[str] = Counter()
    cases = 0
    for case in root.iter("testcase"):
        cases += 1
        name = case.get("file")
        if not name:
            raise CheckError(
                f"{junit_xml}: test case {case.get('name')!r} has no 'file' attribute; "
                "run pytest with -o junit_family=xunit1"
            )
        skips[name] += len(case.findall("skipped"))
    if cases == 0:
        raise CheckError(f"{junit_xml}: the JUnit file holds no test case")
    return skips


def check(
    skips: Counter[str],
    limits: dict[str, int],
    lane: str,
    partial: frozenset[str] | set[str] = frozenset(),
) -> Result:
    """Compare the skips per file with the allowed numbers.

    ``partial`` names the test files that did not run whole. They give no
    "lower the number" notice; a count above the number still fails.
    """
    result = Result()
    for name in sorted(skips):
        allowed = limits.get(name, 0)
        if skips[name] > allowed:
            result.errors.append(
                f"{name} skipped {skips[name]} tests; the {lane} lane allows {allowed} "
                "(tests/_skip_limits.json). Make the tests run, or raise the number "
                "in this pull request."
            )
    for name in sorted(limits):
        if name in skips and name not in partial and skips[name] < limits[name]:
            result.notices.append(
                f"{name} skipped {skips[name]} tests, the number is {limits[name]}: "
                f"lower the number for {name} in this pull request"
            )
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lane", required=True, help=f"one of: {', '.join(LANES)}")
    parser.add_argument("--limits", type=Path, default=LIMITS_FILE, help="the count file")
    parser.add_argument(
        "--without-secrets",
        action="store_true",
        help="a run without the repository secrets: take the without_secrets numbers",
    )
    parser.add_argument(
        "--selection",
        type=Path,
        help="what pytest ran, one test file or test id per line",
    )
    parser.add_argument("junit_xml", type=Path, help="the JUnit file of one shard")
    args = parser.parse_args(argv)
    kind = RUN_KINDS[1] if args.without_secrets else RUN_KINDS[0]
    try:
        limits = load_limits(args.limits, args.lane, args.without_secrets)
        skips = count_skips(args.junit_xml)
        partial = files_picked_by_test_id(args.selection) if args.selection else set()
    except CheckError as exc:
        print(f"::error title=Skip limit check::{exc}")
        return 1
    result = check(skips, limits, args.lane, partial)
    for notice in result.notices:
        print(f"::notice title=Skip limit::{notice}")
    for error in result.errors:
        print(f"::error title=Too many skipped tests::{error}")
    with_skips = sum(1 for count in skips.values() if count)
    print(
        f"{sum(skips.values())} skipped tests in {with_skips} test files "
        f"({args.lane} lane, {kind} numbers)"
    )
    for name in sorted(skips):
        if skips[name] or name in limits:
            print(f"  {name}: {skips[name]} skipped, allowed {limits.get(name, 0)}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
