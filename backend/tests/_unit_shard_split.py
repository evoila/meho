# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Split the unit test files into CI shards by measured time.

CI runs the unit tests as ``TOTAL`` parallel jobs (shards). Each shard
gets a set of test files. This tool decides which files go to which
shard so that every shard needs about the same time.

Rule: sort the files by their measured seconds, slowest first. Give each
file to the shard that has the least time so far. A file that has no
measurement counts as the median of the measured files. Every file also
carries a small fixed cost for import and collection, so files that
measure zero seconds (all tests skipped) still spread over the shards.
Every file lands in exactly one shard, and the same input always gives
the same split.

The measurements live in ``tests/_unit_shard_durations.json``. Refresh
them after the suite changed a lot, or when one shard is much slower
than the others again. Take them from CI, not from a laptop: tests that
need Docker or the spec shelf skip locally but run in CI. Every unit
shard uploads its per-test times as the artifact
``python-unit-shard-junit-<N>``. Download the four files of one green
run and pass them all to ``--measure``::

    gh run download <run-id> --repo evoila/meho \\
        --pattern 'python-unit-shard-junit-*' --dir /tmp/unit-junit
    uv run python tests/_unit_shard_split.py \\
        --measure /tmp/unit-junit/*/pytest-unit-shard.xml \\
        > tests/_unit_shard_durations.json

CI usage (see the ``python-unit-shard`` job in ``.github/workflows/ci.yml``)::

    find tests -name 'test_*.py' ... | sort \\
        | uv run python tests/_unit_shard_split.py --shard 2 --total 4

This is CI tooling, not a test (the leading underscore keeps pytest
from collecting it).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

DURATIONS_FILE = Path(__file__).with_name("_unit_shard_durations.json")
# Import and collection cost per file that the per-test times do not
# contain (collecting all unit files took 49 s for 919 files locally).
FIXED_SECONDS_PER_FILE = 0.1


def load_durations(path: Path = DURATIONS_FILE) -> dict[str, float]:
    """Return the measured seconds per test file from the JSON file."""
    with path.open() as fh:
        data = json.load(fh)
    seconds = data["seconds"]
    if not isinstance(seconds, dict) or not seconds:
        raise ValueError(f"{path}: 'seconds' must be a non-empty object")
    return {str(name): float(value) for name, value in seconds.items()}


def split(files: list[str], durations: dict[str, float], total: int) -> list[list[str]]:
    """Return ``total`` lists of files with about the same summed seconds.

    Files keep a stable order inside each shard (sorted by name) so the
    output does not depend on the order of ``files``.
    """
    if total < 1:
        raise ValueError("total must be at least 1")
    unique = sorted(set(files))
    weighted = sorted(
        ((_seconds(name, durations), name) for name in unique),
        key=lambda item: (-item[0], item[1]),
    )
    shards: list[list[str]] = [[] for _ in range(total)]
    loads = [0.0] * total
    for seconds, name in weighted:
        index = loads.index(min(loads))
        shards[index].append(name)
        loads[index] += seconds
    return [sorted(shard) for shard in shards]


def predicted_seconds(shards: list[list[str]], durations: dict[str, float]) -> list[float]:
    """Return the summed seconds per shard, fixed cost per file included."""
    return [sum(_seconds(name, durations) for name in shard) for shard in shards]


def _seconds(name: str, durations: dict[str, float]) -> float:
    default = statistics.median(durations.values()) if durations else 1.0
    return durations.get(name, default) + FIXED_SECONDS_PER_FILE


def measure(junit_xmls: list[Path]) -> dict[str, float]:
    """Return seconds per test file from pytest JUnit XML files (xunit1 family).

    xunit1 writes a ``file`` attribute on every test case. The seconds of
    all test cases of one file are added up, across all given files.
    """
    per_file: dict[str, float] = defaultdict(float)
    for junit_xml in junit_xmls:
        for case in ET.parse(junit_xml).iter("testcase"):
            name = case.get("file")
            if not name:
                raise ValueError(
                    f"{junit_xml}: test case without a 'file' attribute; "
                    "run pytest with -o junit_family=xunit1"
                )
            per_file[name] += float(case.get("time", "0"))
    return dict(sorted(per_file.items()))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shard", type=int, help="1-based shard number to print")
    parser.add_argument("--total", type=int, default=4, help="number of shards")
    parser.add_argument(
        "--measure",
        type=Path,
        nargs="+",
        metavar="JUNIT_XML",
        help="print a durations JSON built from these pytest JUnit XML files and exit",
    )
    args = parser.parse_args(argv)

    if args.measure is not None:
        seconds = measure(args.measure)
        document = {
            "measured_at": datetime.now(UTC).strftime("%Y-%m-%d"),
            "how": "tests/_unit_shard_split.py --measure <pytest junit xml files>",
            "seconds": {name: round(value, 2) for name, value in seconds.items()},
        }
        json.dump(document, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0

    if args.shard is None or not 1 <= args.shard <= args.total:
        parser.error("--shard must be between 1 and --total")
    files = [line.strip() for line in sys.stdin if line.strip()]
    durations = load_durations()
    shards = split(files, durations, args.total)
    unmeasured = sum(1 for name in files if name not in durations)
    predicted = predicted_seconds(shards, durations)
    print(
        f"shard {args.shard}/{args.total}: {len(shards[args.shard - 1])} test files, "
        f"predicted {predicted[args.shard - 1]:.0f} s of {sum(predicted):.0f} s "
        f"(all shards: {', '.join(f'{p:.0f}' for p in predicted)} s; "
        f"{unmeasured} files without a measurement)",
        file=sys.stderr,
    )
    for name in shards[args.shard - 1]:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
