#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Measure how many tests a "run only the tests for the changed code" map would pick (#5298).

This script only measures. CI does not use it, and it changes nothing in
which tests CI runs.

Input:

* A combined coverage data file made with ``dynamic_context =
  test_function``. Each line of code then carries the names of the test
  functions that ran it. The map is: source file -> tests that ran it.
* The list of collected unit tests, from ``pytest --collect-only -q``.
  Coverage knows test functions; pytest runs test items (one function
  can be many items, one per parameter set). The list turns functions
  into items and gives the full suite size. **Make this list on the exact
  commit the coverage run tested.** For a ``pull_request`` run that is the
  merge commit, not the pull request head; the log of the checkout step
  shows its SHA ("HEAD is now at ..."). If the two inputs come from
  different commits, the counts are wrong. The script counts the test
  functions in the coverage data that are not in the list and warns when
  that count is not zero. A small count can also come from tests that a
  decorator wraps: coverage then names them after the decorator's module
  (for example ``functools.test_x``).
* The files changed by the last N merged pull requests, read with ``gh``.
  ``gh pr list`` orders by creation date, and no GitHub API orders by
  merge date. So the script reads a larger window of merged pull requests
  (``_WINDOW`` times N, newest created first), sorts them by merge date
  and keeps the N merged last. A pull request created before that window
  and merged recently is still left out.

For each pull request it counts the test items the map would pick, in two
ways:

* **map only** (best case): a changed source file picks the tests that ran
  it, a changed test file picks its own tests, and every other file picks
  nothing.
* **safe**: like "map only", but a changed file that the map cannot judge
  picks the full suite. Docs (``docs/`` or ``*.md``) never pick tests,
  like the docs-only rule in CI today. The map cannot judge four kinds of
  files, and the report counts them apart:

  * **not in data**: a source or test file that the coverage data or the
    test list does not have, for example a file added after the coverage
    run.
  * **no test ran**: a source file that is in the coverage data, but no
    test function ran any of its lines (its code runs only at import time
    or in fixtures).
  * **test not in list**: a source file that a test function ran which
    the test list does not have (a decorator-wrapped test, or inputs from
    different commits). The map cannot turn that name into test items.
  * **other**: a config or data file, a test helper or ``conftest.py``, or
    a file outside ``backend/``.

It also lists the source files that almost every test runs.

Usage (from ``backend/``, so ``coverage`` is installed). Run the collect
step in a checkout of the commit the coverage run tested::

    git worktree add /tmp/tested <tested-commit>
    (cd /tmp/tested/backend && uv run pytest --collect-only -q tests \\
        --ignore=tests/integration) > /tmp/collected.txt
    uv run python ../scripts/ci/measure_test_map.py \\
        --data .coverage --collected /tmp/collected.txt --last 30
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_SOURCE = re.compile(r"^backend/src/.+\.py$")
_TEST_FILE = re.compile(r"^backend/tests/(?!integration/)(?:.+/)?test_[^/]+\.py$")
_INTEGRATION = re.compile(r"^backend/tests/integration/")
_DOCS = re.compile(r"(^docs/|\.md$)")
# How many merged pull requests to read per pull request asked for, so the
# ones merged last are in the window even when they were created earlier.
_WINDOW = 10


@dataclass
class Suite:
    """The collected unit test items, indexed two ways."""

    items: list[str]
    by_function: dict[str, list[str]] = field(default_factory=dict)
    by_file: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_nodeids(cls, nodeids: Iterable[str]) -> Suite:
        suite = cls(items=[])
        by_function: dict[str, list[str]] = defaultdict(list)
        by_file: dict[str, list[str]] = defaultdict(list)
        for nodeid in nodeids:
            suite.items.append(nodeid)
            by_function[function_key(nodeid)].append(nodeid)
            by_file["backend/" + nodeid.split("::", 1)[0]].append(nodeid)
        suite.by_function = dict(by_function)
        suite.by_file = dict(by_file)
        return suite


def function_key(nodeid: str) -> str:
    """Turn a pytest node id into the name coverage's test_function context uses.

    ``tests/a/test_b.py::TestC::test_d[x]`` -> ``tests.a.test_b.TestC.test_d``.
    """
    path, _, rest = nodeid.partition("::")
    module = path.removesuffix(".py").replace("/", ".")
    name = rest.split("[", 1)[0].replace("::", ".")
    return f"{module}.{name}"


def read_collected(text: str) -> list[str]:
    """Keep the node id lines of ``pytest --collect-only -q`` output."""
    return [line.strip() for line in text.splitlines() if "::" in line and not line.startswith(" ")]


def load_map(data_file: str) -> dict[str, set[str]]:
    """Read the coverage data into: repo path of a source file -> test functions that ran it."""
    from coverage import CoverageData

    data = CoverageData(data_file)
    data.read()
    file_map: dict[str, set[str]] = {}
    for measured in data.measured_files():
        contexts: set[str] = set()
        for names in data.contexts_by_lineno(measured).values():
            contexts.update(name for name in names if name)
        file_map["backend/" + measured] = contexts
    return file_map


@dataclass
class Selection:
    map_only: set[str]
    not_in_data: list[str] = field(default_factory=list)
    no_test_ran: list[str] = field(default_factory=list)
    test_not_in_list: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    @property
    def safe_full(self) -> bool:
        return bool(self.not_in_data or self.no_test_ran or self.test_not_in_list or self.other)


def select(changed: Iterable[str], file_map: dict[str, set[str]], suite: Suite) -> Selection:
    sel = Selection(map_only=set())
    for path in changed:
        if _DOCS.search(path) or _INTEGRATION.match(path):
            continue
        if _TEST_FILE.match(path):
            if path in suite.by_file:
                sel.map_only.update(suite.by_file[path])
            else:
                sel.not_in_data.append(path)
            continue
        if not _SOURCE.match(path):
            sel.other.append(path)
        elif path not in file_map:
            sel.not_in_data.append(path)
        elif not file_map[path]:
            sel.no_test_ran.append(path)
        elif not file_map[path] <= suite.by_function.keys():
            sel.test_not_in_list.append(path)
        else:
            for function in file_map[path]:
                sel.map_only.update(suite.by_function[function])
    return sel


def unmatched_functions(file_map: dict[str, set[str]], suite: Suite) -> set[str]:
    """Return the test functions in the coverage data that the test list does not have.

    Not empty means the two inputs come from different commits.
    """
    in_data: set[str] = set().union(*file_map.values()) if file_map else set()
    return in_data - suite.by_function.keys()


def touched_by_most(file_map: dict[str, set[str]], top: int) -> tuple[int, list[tuple[str, float]]]:
    """Return the number of test functions in the map and the files most tests run."""
    all_functions = set().union(*file_map.values()) if file_map else set()
    total = len(all_functions)
    shares = sorted(
        ((path, len(fns) / total) for path, fns in file_map.items() if total),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return total, shares[:top]


def merged_last(listed: list[dict[str, Any]], last: int) -> list[dict[str, Any]]:
    """Keep the ``last`` pull requests merged most recently, newest merge first.

    ``listed`` is in the order ``gh pr list`` gives, newest created first,
    which is not the merge order.
    """
    return sorted(listed, key=lambda pr: pr["mergedAt"], reverse=True)[:last]


def fetch_prs(repo: str, last: int) -> list[tuple[int, str, list[str]]]:
    """Return (number, title, changed files) for the last merged pull requests."""
    listed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--limit",
            str(last * _WINDOW),
            "--json",
            "number,title,mergedAt",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    prs = []
    for pr in merged_last(json.loads(listed.stdout), last):
        files = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/pulls/{pr['number']}/files",
                "--jq",
                ".[] | .filename, (.previous_filename // empty)",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        prs.append((pr["number"], pr["title"], files.stdout.splitlines()))
    return prs


def _stats(values: list[int]) -> str:
    return f"median {statistics.median(values):g}, smallest {min(values)}, largest {max(values)}"


def _shown(paths: list[str]) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:3])
    return shown + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")


def report(
    prs: list[tuple[int, str, list[str]]], file_map: dict[str, set[str]], suite: Suite, top: int
) -> str:
    full = len(suite.items)
    unmatched = unmatched_functions(file_map, suite)
    lines = [
        f"Full unit suite: {full} test items.",
        f"Test functions in the coverage data that are not in the test list: {len(unmatched)}.",
        "",
        "| PR | files | map only | safe | not in data | no test ran | test not in list | other |",
        "|---|---|---|---|---|---|---|---|",
    ]
    map_counts: list[int] = []
    safe_counts: list[int] = []
    # Safe count if "no test ran" files were handled some other way
    # (for example by import analysis), so only the other two kinds fall back.
    without_no_test_ran: list[int] = []
    reasons = {"not in data": 0, "no test ran": 0, "test not in list": 0, "other": 0}
    for number, _title, files in prs:
        sel = select(files, file_map, suite)
        map_count = len(sel.map_only)
        map_counts.append(map_count)
        safe_counts.append(full if sel.safe_full else map_count)
        without_no_test_ran.append(
            full if sel.not_in_data or sel.test_not_in_list or sel.other else map_count
        )
        reasons["not in data"] += bool(sel.not_in_data)
        reasons["no test ran"] += bool(sel.no_test_ran)
        reasons["test not in list"] += bool(sel.test_not_in_list)
        reasons["other"] += bool(sel.other)
        lines.append(
            f"| #{number} | {len(files)} | {map_count} | {safe_counts[-1]} "
            f"| {_shown(sel.not_in_data)} | {_shown(sel.no_test_ran)} "
            f"| {_shown(sel.test_not_in_list)} | {_shown(sel.other)} |"
        )
    if prs:
        n_full = sum(1 for count in safe_counts if count == full and full)
        n_full_without = sum(1 for count in without_no_test_ran if count == full and full)
        lines += [
            "",
            f"- Map only: {_stats(map_counts)}.",
            f"- Safe: {_stats(safe_counts)}. Full suite for {n_full} of {len(prs)}.",
            "- Pull requests with a file of each kind the map cannot judge: "
            + ", ".join(f"{name}: {count}" for name, count in reasons.items())
            + ".",
            f'- Safe if "no test ran" files were handled another way: '
            f"{_stats(without_no_test_ran)}. Full suite for {n_full_without} of {len(prs)}.",
        ]
    total, shares = touched_by_most(file_map, top)
    never = sum(1 for path, fns in file_map.items() if not fns)
    lines += [
        "",
        f"Test functions in the map: {total}.",
        f"Source files that no test function ran: {never} of {len(file_map)}.",
        "",
        "| source file | share of test functions that run it |",
        "|---|---|",
    ]
    lines += [f"| `{path}` | {share:.0%} |" for path, share in shares]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data", required=True, help="combined coverage data file with test contexts"
    )
    parser.add_argument("--collected", required=True, help="output of pytest --collect-only -q")
    parser.add_argument("--repo", default="evoila/meho")
    parser.add_argument("--last", type=int, default=30, help="number of merged pull requests")
    parser.add_argument("--top", type=int, default=25, help="number of most-run files to list")
    args = parser.parse_args(argv)

    with open(args.collected, encoding="utf-8") as fh:
        suite = Suite.from_nodeids(read_collected(fh.read()))
    file_map = load_map(args.data)
    unmatched = unmatched_functions(file_map, suite)
    if unmatched:
        print(
            f"WARNING: {len(unmatched)} test functions in the coverage data are not in the "
            "test list, for example "
            + ", ".join(sorted(unmatched)[:3])
            + ". Make the test list on the exact commit the coverage run tested. A few"
            " names can also differ when a decorator wraps the test (for example"
            " functools.test_x). A changed file that such a test ran picks the full suite.",
            file=sys.stderr,
        )
    print(report(fetch_prs(args.repo, args.last), file_map, suite, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
