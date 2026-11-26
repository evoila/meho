# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail a CI shard when a test file skips more tests than allowed.

A skipped test shows as green in CI. So a test that stops running (a
missing tool, a missing setting) stays hidden. This tool reads the JUnit
file of one shard and counts the skipped tests per test file. Each count
must not be above the number for that file in ``tests/_skip_limits.json``.

- The count file holds one number per test file and lane (``unit`` or
  ``integration``). A test file that is not in it may skip 0 tests.
- A pull request that adds a skip raises the number in the same pull
  request, so the raise is visible in the diff.
- A file that skips fewer tests than its number gives a notice ("lower
  the number"), not a failure. Some integration skips depend on how fast
  pods start, and a lucky run must not fail a pull request.
- Expected failures (xfail) count as skips: JUnit writes both as
  ``<skipped>``.

The count is per test file, not per shard: the shard split moves files
between shards, but a file always runs whole in one shard. So each shard
checks its own JUnit file, and a short run (one connector, or the tests
picked by the test map) passes when no file is above its number.

The tool fails on the safe side: a missing, empty or broken JUnit file, a
missing or broken count file, an unknown lane, or a row for a test file
that is not in the repo all fail the check.

CI usage (see ``python-unit-shard`` and ``python-integration-shard`` in
``.github/workflows/ci.yml``), from ``backend/``::

    uv run python tests/_skip_limit_check.py --lane unit pytest-unit-shard.xml

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


def load_limits(path: Path, lane: str, backend_dir: Path = BACKEND_DIR) -> dict[str, int]:
    """Return the allowed skips per test file for ``lane``.

    Raises ``CheckError`` when the file is missing or broken, names an
    unknown lane, holds a number that is not a whole number of 0 or more,
    or names a test file that is not in the repo.
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
        for name, number in rows.items():
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                problems.append(
                    f"{lane_name}: {name}: the number must be a whole number of 0 or more"
                )
            elif not (backend_dir / name).is_file():
                problems.append(f"{lane_name}: {name} is not in the repo; remove this row")
            elif lane_name == lane:
                limits[name] = number
    if problems:
        raise CheckError(f"{path}: " + "; ".join(problems))
    return limits


def count_skips(junit_xml: Path) -> Counter[str]:
    """Return the number of skipped tests per test file in one JUnit file.

    Needs the xunit1 family (``-o junit_family=xunit1``), which writes the
    test file name on every test case. Raises ``CheckError`` when the file
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
        skipped = len(case.findall("skipped"))
        if skipped:
            skips[name] += skipped
    if cases == 0:
        raise CheckError(f"{junit_xml}: the JUnit file holds no test case")
    return skips


def check(skips: Counter[str], limits: dict[str, int], lane: str) -> Result:
    """Compare the skips per file with the allowed numbers."""
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
        if name in skips and skips[name] < limits[name]:
            result.notices.append(
                f"{name} skipped {skips[name]} tests, the number is {limits[name]}: "
                f"lower the number for {name} in this pull request"
            )
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lane", required=True, help=f"one of: {', '.join(LANES)}")
    parser.add_argument("--limits", type=Path, default=LIMITS_FILE, help="the count file")
    parser.add_argument("junit_xml", type=Path, help="the JUnit file of one shard")
    args = parser.parse_args(argv)
    try:
        limits = load_limits(args.limits, args.lane)
        skips = count_skips(args.junit_xml)
    except CheckError as exc:
        print(f"::error title=Skip limit check::{exc}")
        return 1
    result = check(skips, limits, args.lane)
    for notice in result.notices:
        print(f"::notice title=Skip limit::{notice}")
    for error in result.errors:
        print(f"::error title=Too many skipped tests::{error}")
    print(f"{sum(skips.values())} skipped tests in {len(skips)} test files ({args.lane} lane)")
    for name in sorted(skips):
        print(f"  {name}: {skips[name]} skipped, allowed {limits.get(name, 0)}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
