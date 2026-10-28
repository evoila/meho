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
  into items and gives the full suite size.
* The files changed by the last N merged pull requests, read with ``gh``.

For each pull request it counts the test items the map would pick, in two
ways:

* **map only** (best case): a changed source file picks the tests that ran
  it, a changed test file picks its own tests, and every other file picks
  nothing.
* **safe**: like "map only", but a changed file that the map cannot judge
  picks the full suite. Docs (``docs/`` or ``*.md``) never pick tests,
  like the docs-only rule in CI today. "Cannot judge" means: a config or
  data file, a test helper or ``conftest.py``, a file outside ``backend/``,
  or a source file that no test function ran (for example a new file).

It also lists the source files that almost every test runs.

Usage (from ``backend/``, so ``coverage`` is installed)::

    uv run pytest --collect-only -q tests --ignore=tests/integration > /tmp/collected.txt
    uv run python ../scripts/ci/measure_test_map.py \\
        --data .coverage --collected /tmp/collected.txt --last 30
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

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
    safe_full: bool
    cannot_judge: list[str]


def select(changed: Iterable[str], file_map: dict[str, set[str]], suite: Suite) -> Selection:
    picked: set[str] = set()
    cannot_judge: list[str] = []
    for path in changed:
        if _DOCS.search(path) or _INTEGRATION.match(path):
            continue
        if _TEST_FILE.match(path) and path in suite.by_file:
            picked.update(suite.by_file[path])
            continue
        functions = file_map.get(path, set()) if _SOURCE.match(path) else set()
        if not functions:
            cannot_judge.append(path)
            continue
        for function in functions:
            picked.update(suite.by_function.get(function, ()))
    return Selection(map_only=picked, safe_full=bool(cannot_judge), cannot_judge=cannot_judge)


def touched_by_most(file_map: dict[str, set[str]], top: int) -> tuple[int, list[tuple[str, float]]]:
    """Return the number of test functions in the map and the files most tests run."""
    all_functions = set().union(*file_map.values()) if file_map else set()
    total = len(all_functions)
    shares = sorted(
        ((path, len(fns) / total) for path, fns in file_map.items() if total),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return total, shares[:top]


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
            str(last),
            "--json",
            "number,title",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    prs = []
    for pr in json.loads(listed.stdout):
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
        prs.append((pr["number"], pr["title"], files.stdout.split()))
    return prs


def _stats(values: list[int]) -> str:
    return f"median {statistics.median(values):g}, smallest {min(values)}, largest {max(values)}"


def report(
    prs: list[tuple[int, str, list[str]]], file_map: dict[str, set[str]], suite: Suite, top: int
) -> str:
    full = len(suite.items)
    lines = [
        f"Full unit suite: {full} test items.",
        "",
        "| PR | files | map only | safe | files the map cannot judge |",
        "|---|---|---|---|---|",
    ]
    map_counts: list[int] = []
    safe_counts: list[int] = []
    for number, _title, files in prs:
        sel = select(files, file_map, suite)
        map_count = len(sel.map_only)
        safe_count = full if sel.safe_full else map_count
        map_counts.append(map_count)
        safe_counts.append(safe_count)
        shown = ", ".join(f"`{p}`" for p in sel.cannot_judge[:3])
        more = f" and {len(sel.cannot_judge) - 3} more" if len(sel.cannot_judge) > 3 else ""
        lines.append(f"| #{number} | {len(files)} | {map_count} | {safe_count} | {shown}{more} |")
    if prs:
        lines += ["", f"- Map only: {_stats(map_counts)}.", f"- Safe: {_stats(safe_counts)}."]
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
    print(report(fetch_prs(args.repo, args.last), file_map, suite, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
