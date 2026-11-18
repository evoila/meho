#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Fail when a line a pull request adds holds a word of the private name list.

The list is not in this repository. It comes from the environment variable
``PRIVATE_NAME_LIST`` (in CI: a repository secret). Each non-empty line of
the list is one pattern, matched without regard to case. A plain word is a
pattern too. A word with a special character needs a backslash before it.

The script reads only the lines the change adds (``git diff <base>..HEAD``),
in every file except ``CHANGELOG.md``. On a hit it prints the file and the
line number and one fixed sentence. It never prints the word, the pattern or
the line, and it never says which entry of the list matched.

Exit codes: 0 = no hit (or no list set, with a note), 1 = at least one hit,
2 = the script could not run (bad list, git failure, no base).

Usage: check_private_names.py --base <git rev> [--require-list]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator

LIST_ENV = "PRIVATE_NAME_LIST"
EXCLUDED_FILES = frozenset({"CHANGELOG.md"})
HIT_MESSAGE = "this line holds a word of the private name list"

_FILE_HEADER = re.compile(r"^\+\+\+ (?:b/(?P<path>.*)|/dev/null)$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


class ListError(Exception):
    """The list is not usable. The message never holds list content."""


def compile_list(raw: str) -> re.Pattern[str] | None:
    """One pattern from the list text, or None when the list is empty."""
    entries = [line.strip() for line in raw.splitlines() if line.strip()]
    if not entries:
        return None
    try:
        return re.compile("|".join(f"(?:{entry})" for entry in entries), re.IGNORECASE)
    except re.error:
        # The error text of `re` can quote the pattern, so it is not shown.
        raise ListError(
            f"the {LIST_ENV} list is not a valid pattern list; "
            "an entry may need a backslash before a special character"
        ) from None


def added_lines(diff: str) -> Iterator[tuple[str, int, str]]:
    """Yield (path, line number, text) for each added line of a zero-context diff."""
    path: str | None = None
    number = 0
    for line in diff.splitlines():
        header = _FILE_HEADER.match(line)
        if header is not None:
            path = header.group("path")
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk is not None:
            number = int(hunk.group("start"))
            continue
        if path is None or not line.startswith("+"):
            continue
        yield path, number, line[1:]
        number += 1


def find_hits(diff: str, pattern: re.Pattern[str]) -> list[tuple[str, int]]:
    return [
        (path, number)
        for path, number, text in added_lines(diff)
        if path not in EXCLUDED_FILES and pattern.search(text)
    ]


def report(hits: Iterable[tuple[str, int]]) -> None:
    for path, number in hits:
        print(f"::error file={path},line={number}::{HIT_MESSAGE}")
        print(f"{path}:{number}: {HIT_MESSAGE}")


def read_diff(base: str) -> str:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--diff-filter=ACMR",
            f"{base}..HEAD",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="git revision to diff against")
    parser.add_argument(
        "--require-list",
        action="store_true",
        help="fail when the list is not set (the merge queue of this repository)",
    )
    args = parser.parse_args(argv)

    try:
        pattern = compile_list(os.environ.get(LIST_ENV, ""))
    except ListError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if pattern is None:
        if args.require_list:
            print(
                f"error: the secret {LIST_ENV} is not set, so the check cannot run",
                file=sys.stderr,
            )
            return 1
        print(
            f"::warning::The secret {LIST_ENV} is not set, so the private name check did not run."
        )
        print(f"note: {LIST_ENV} is not set; no lines were checked.")
        return 0

    try:
        diff = read_diff(args.base)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    hits = find_hits(diff, pattern)
    if hits:
        report(hits)
        print(f"{len(hits)} added line(s) hold a word of the private name list.")
        print("Ask a maintainer which word it is, then write the line again without it.")
        return 1
    print("No added line holds a word of the private name list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
