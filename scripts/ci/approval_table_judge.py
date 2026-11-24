#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Judge the approval table of a pull request against the base branch (docs/codebase/approvals.md).

  --judge BASE HEAD --approved FILE [--base-approved FILE]
      BASE and HEAD are two copies of backend/tests/snapshots/approval_table.json.
      Exit 1 on a change that the approved file does not list. Only the
      entries that are not in the base's approved file count.

The judge uses only the standard library. The CI job reads this script from
the base branch, so a pull request cannot weaken the rule that judges it.

A row is one operation of one connector. A change is one of:
  lowered_level  the risk level is lower (destructive > dangerous > caution > safe)
  lost_approval  "needs approval" went from true to false
  lost_never     no grant could cover the call before, and one can now
  left_credential  the class was credential_read, credential_write or
                 credential_mint, and is now another class
  removed        the base has the row, the head does not
A new row passes, and so does a change toward more approval or a higher level.

An approved entry names one change (the shape of the file's header):

  - op_id: vault.kv.put
    connector_id: vault-1.x
    change: lowered_level
    reason: one line that says why the change is wanted
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LEVELS = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}
CREDENTIAL_CLASSES = {"credential_read", "credential_write", "credential_mint"}


def load(path: Path) -> dict[tuple[str, str], dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {(row["connector_id"], row["op_id"]): row for row in rows}


def level(row: dict) -> int:
    name = row["risk_level"]
    if name not in LEVELS:
        sys.exit(f"unknown risk level {name!r} for {row['op_id']} ({row['connector_id']})")
    return LEVELS[name]


def differences(base: dict, head: dict) -> list[tuple[str, str, str]]:
    """The changes as (op_id, connector_id, kind). New rows are not compared."""
    found = [(key[1], key[0], "removed") for key in sorted(base.keys() - head.keys())]
    for key in sorted(base.keys() & head.keys()):
        old, new = base[key], head[key]
        kinds = []
        if level(new) < level(old):
            kinds.append("lowered_level")
        if old["needs_approval"] and not new["needs_approval"]:
            kinds.append("lost_approval")
        if old["never_covered"] is not None and new["never_covered"] is None:
            kinds.append("lost_never")
        if old["class"] in CREDENTIAL_CLASSES and new["class"] not in CREDENTIAL_CLASSES:
            kinds.append("left_credential")
        found += [(key[1], key[0], kind) for kind in kinds]
    return found


def read_approved(path: Path) -> list[dict]:
    """The approved file: ``approved:`` then a list of entries, in the simple form of the header."""
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
    base_path: Path,
    head_path: Path,
    approved_path: Path,
    base_approved_path: Path | None = None,
) -> int:
    """Only the entries that the pull request adds can approve a change or be stale."""
    old = []
    if base_approved_path is not None and base_approved_path.is_file():
        old = read_approved(base_approved_path)
    approved = [e for e in read_approved(approved_path) if e not in old]
    for entry in approved:
        if not entry.get("reason", "").strip():
            sys.exit(f"approved entry {entry} has no reason")
    found = differences(load(base_path), load(head_path))
    used = set()
    not_approved = 0
    for op_id, connector_id, kind in found:
        match = next(
            (
                i
                for i, e in enumerate(approved)
                if e.get("op_id") == op_id
                and e.get("connector_id") == connector_id
                and e.get("change") == kind
            ),
            None,
        )
        if match is None:
            print(f"{op_id} ({connector_id}): {kind}")
            not_approved += 1
        else:
            used.add(match)
    stale = [entry for index, entry in enumerate(approved) if index not in used]
    for entry in stale:
        print(f"approved entry matches no change (stale): {entry}")
    print(f"{len(found)} change(s), {not_approved + len(stale)} not approved")
    return 1 if not_approved or stale else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--judge", nargs=2, metavar=("BASE", "HEAD"), required=True)
    parser.add_argument("--approved", required=True)
    parser.add_argument("--base-approved")
    args = parser.parse_args()
    base_approved = Path(args.base_approved) if args.base_approved else None
    return judge(Path(args.judge[0]), Path(args.judge[1]), Path(args.approved), base_approved)


if __name__ == "__main__":
    sys.exit(main())
