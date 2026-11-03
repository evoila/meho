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
  No GitHub API orders pull requests by merge date. The script reads the
  closed pull requests page by page, most recently updated first. A merge
  updates a pull request, so its update time is never before its merge
  time. Reading stops at the first page whose oldest update time is not
  after the N-th newest merge time found so far: no later page can hold a
  pull request merged after it. This finds the N merged last, also when
  they were created long ago. ``--merged-before`` keeps only pull requests
  merged before a time, so the sample stays the same when the script runs
  again later on the same coverage data.

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
    The tests of the file that are in the list still count in "map only".
  * **other**: a config or data file, a test helper or ``conftest.py``, or
    a file outside ``backend/``.

It also lists the source files that almost every test runs.

Usage (from ``backend/``, so ``coverage`` is installed). Run the collect
step in a checkout of the commit the coverage run tested::

    git worktree add /tmp/tested <tested-commit>
    (cd /tmp/tested/backend && uv run pytest --collect-only -q tests \\
        --ignore=tests/integration) > /tmp/collected.txt
    uv run python ../scripts/ci/measure_test_map.py \\
        --data .coverage --collected /tmp/collected.txt --last 30 \\
        --merged-before <time the coverage run started, like 2026-10-28T22:02:55Z>
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

_SOURCE = re.compile(r"^backend/src/.+\.py$")
_TEST_FILE = re.compile(r"^backend/tests/(?!integration/)(?:.+/)?test_[^/]+\.py$")
_INTEGRATION = re.compile(r"^backend/tests/integration/")
_DOCS = re.compile(r"(^docs/|\.md$)")


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
        else:
            known = file_map[path] & suite.by_function.keys()
            if known != file_map[path]:
                sel.test_not_in_list.append(path)
            for function in known:
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


def _gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def closed_pr_pages(repo: str) -> Iterator[list[dict[str, Any]]]:
    """Yield the closed pull requests page by page, most recently updated first."""
    page = 1
    while prs := json.loads(
        _gh(
            "api",
            f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100&page={page}",
        )
    ):
        yield prs
        page += 1


def merged_last(
    pages: Iterable[list[dict[str, Any]]], last: int, before: str | None = None
) -> list[dict[str, Any]]:
    """Keep the ``last`` pull requests merged most recently, newest merge first.

    ``pages`` gives closed pull requests page by page, most recently updated
    first. A merge updates a pull request, so its update time is never
    before its merge time. Reading stops at the first page whose oldest
    update time is not after the ``last``-th newest merge time found so
    far, because no later page can hold a pull request merged after it.
    ``before`` keeps only pull requests merged before that time.
    """
    kept: list[dict[str, Any]] = []
    for page in pages:
        kept += [
            pr for pr in page if pr["merged_at"] and (before is None or pr["merged_at"] < before)
        ]
        kept.sort(key=lambda pr: pr["merged_at"], reverse=True)
        del kept[last:]
        if len(kept) == last and page[-1]["updated_at"] <= kept[-1]["merged_at"]:
            break
    return kept


def fetch_prs(repo: str, last: int, before: str | None) -> list[tuple[int, str, list[str]]]:
    """Return (number, title, changed files) for the last merged pull requests."""
    prs = []
    for pr in merged_last(closed_pr_pages(repo), last, before):
        files = _gh(
            "api",
            "--paginate",
            f"repos/{repo}/pulls/{pr['number']}/files",
            "--jq",
            ".[] | .filename, (.previous_filename // empty)",
        )
        prs.append((pr["number"], pr["title"], files.splitlines()))
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
    parser.add_argument(
        "--merged-before",
        help="keep only pull requests merged before this time (ISO 8601, UTC, like "
        "2026-10-28T22:02:55Z); use the time the coverage run started",
    )
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
    prs = fetch_prs(args.repo, args.last, args.merged_before)
    print(report(prs, file_map, suite, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
