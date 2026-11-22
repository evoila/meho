#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Write and judge the secret-hiding snapshot (docs/codebase/redaction.md, "Compare with main").

  --write                    write backend/tests/snapshots/redaction_compare.jsonl
                             from the current code (run from backend/ with uv run)
  --judge BASE HEAD --approved FILE [--base-approved FILE]
                             compare two snapshots; exit 1 on a difference that
                             the approved file does not list. Only the entries
                             that are not in the base's approved file count.

The judge uses only the standard library. The CI job reads this script from
the base branch, so a pull request cannot weaken the rule that judges it.

A part id starting with "s" is a secret value; "p" is a plain word. A
difference is one of:
  shown:<part>   the base hides the part, the head shows it
  hidden:<part>  a plain part the base shows is now hidden
  text           the answer changed in another way; when a part is hidden or
                 shown, the answer without its planted parts ("rest") is
                 compared, so this is also found next to a part difference
  removed        the base has the input, the head snapshot does not
A secret part that the base showed and the head hides passes: that is a fix.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SNAPSHOT = Path("tests/snapshots/redaction_compare.jsonl")


def load(path: Path) -> dict[tuple[str, str], dict]:
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[(record["id"], record["path"])] = record
    return records


def differences(base: dict, head: dict) -> list[tuple[str, str, str]]:
    """The differences as (id, path, kind). Inputs only in the head are not compared."""
    found = [(*key, "removed") for key in sorted(base.keys() - head.keys())]
    for key in sorted(base.keys() & head.keys()):
        old, new = base[key], head[key]
        shown = sorted(set(old["hidden"]) & set(new["shown"]))
        hidden = sorted(set(old["shown"]) & set(new["hidden"]))
        found += [(*key, f"shown:{part}") for part in shown]
        found += [(*key, f"hidden:{part}") for part in hidden if not part.startswith("s")]
        # A part that is hidden or shown changes the whole answer, so then compare
        # the answer without its planted parts ("rest"): any other change still counts,
        # also next to a part difference, and needs its own approved entry.
        field = "rest" if shown or hidden else "sha"
        if old.get(field) is None or old.get(field) != new.get(field):
            found.append((*key, "text"))
    return found


def read_approved(path: Path) -> list[dict]:
    """The approved file: ``approved:`` then a list of entries, in the simple form of the file's header."""
    entries: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line in ("approved:", "approved: []"):
            continue
        if line.startswith("- "):
            entries.append({})
            line = line[2:]
        key, colon, value = line.partition(":")
        if not entries or not colon:
            sys.exit(f"approved file: cannot read the line {raw!r}")
        entries[-1][key.strip()] = value.strip().strip("\"'")
    return entries


def judge(
    base_path: Path, head_path: Path, approved_path: Path, base_approved_path: Path | None = None
) -> int:
    """Only the entries that the pull request adds can approve a difference or be stale.

    An entry that is also in the base's approved file was merged with an
    earlier pull request: it approves nothing and never fails the job.
    """
    old = []
    if base_approved_path is not None and base_approved_path.is_file():
        old = read_approved(base_approved_path)
    approved = [e for e in read_approved(approved_path) if e not in old]
    for entry in approved:
        if not entry.get("reason"):
            sys.exit(f"approved entry {entry} has no reason")
    found = differences(load(base_path), load(head_path))
    used = set()
    failed = 0
    for input_id, path, kind in found:
        what = kind.split(":", 1)[-1]
        match = next(
            (
                i
                for i, e in enumerate(approved)
                if e.get("id") == input_id and e.get("path") == path and e.get("part") == what
            ),
            None,
        )
        if match is None:
            print(f"{input_id} {path}: {kind}")
            failed += 1
        else:
            used.add(match)
    for index, entry in enumerate(approved):
        if index not in used:
            print(f"approved entry matches no difference (stale): {entry}")
            failed += 1
    print(f"{len(found)} difference(s), {failed} not approved")
    return 1 if failed else 0


def write() -> None:
    sys.path.insert(0, os.getcwd())
    from tests.redaction_compare.answers import snapshot_lines

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text("\n".join(snapshot_lines()) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--judge", nargs=2, metavar=("BASE", "HEAD"))
    parser.add_argument("--approved")
    parser.add_argument("--base-approved")
    args = parser.parse_args()
    if args.write:
        write()
        return 0
    if args.judge and args.approved:
        base_approved = Path(args.base_approved) if args.base_approved else None
        return judge(Path(args.judge[0]), Path(args.judge[1]), Path(args.approved), base_approved)
    parser.error("use --write, or --judge BASE HEAD --approved FILE")
    return 2


if __name__ == "__main__":
    sys.exit(main())
