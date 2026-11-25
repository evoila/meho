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
import re
import sys
from pathlib import Path

LEVELS = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}
CREDENTIAL_CLASSES = {"credential_read", "credential_write", "credential_mint"}
# The first character of a YAML value that the judge does not read.
_UNSUPPORTED_START = set("|>&*!%@`[]{},?")
# The plain values that YAML reads as null.
_NULLS = {"~", "null", "Null", "NULL"}


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


def _value(text: str) -> str | None:
    """A YAML scalar: quoted text, or plain text without a ``#`` comment.

    A comment starts at a ``#`` after a space or a tab. A plain ``~``,
    ``null``, ``Null`` or ``NULL`` is null (``None``); in quotes it is text.
    Any other YAML form
    (a block such as ``|`` or ``>``, an anchor, a tag, a list or a map) stops
    the judge, so the value it reads is always the value YAML reads.
    """
    text = text.strip(" \t")
    if text[:1] == "'":
        value, rest = _single_quoted(text)
    elif text[:1] == '"':
        value, rest = _double_quoted(text)
    else:
        plain = re.split(r"[ \t]#|^#", text, maxsplit=1)[0].rstrip(" \t")
        if plain[:1] and plain[0] in _UNSUPPORTED_START:
            sys.exit(f"approved file: unsupported YAML value {text!r}")
        return None if plain in _NULLS else plain
    rest = rest.lstrip(" \t")
    if rest and not rest.startswith("#"):
        sys.exit(f"approved file: unsupported YAML value {text!r}")
    return value


def _single_quoted(text: str) -> tuple[str, str]:
    """``'It''s here.'`` is ``It's here.``: two apostrophes stand for one."""
    index = 1
    while True:
        end = text.find("'", index)
        if end < 0:
            sys.exit(f"approved file: no closing quote in {text!r}")
        if text[end + 1 : end + 2] != "'":
            return text[1:end].replace("''", "'"), text[end + 1 :]
        index = end + 2


def _double_quoted(text: str) -> tuple[str, str]:
    """The text up to the closing quote that no backslash escapes."""
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            try:
                value = json.loads(text[: index + 1])
            except json.JSONDecodeError:
                sys.exit(f"approved file: unsupported escape in {text!r}")
            return str(value), text[index + 1 :]
        index += 1
    raise SystemExit(f"approved file: no closing quote in {text!r}")


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
        entries[-1][key.strip()] = _value(value)
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
        if not (entry.get("reason") or "").strip():
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
