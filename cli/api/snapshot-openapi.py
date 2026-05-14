#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# Exports the backplane's OpenAPI document and writes a generator-
# friendly snapshot at cli/api/openapi.json.
#
# FastAPI emits OpenAPI 3.1 by default; oapi-codegen v2 doesn't yet
# support 3.1 (upstream issue #373), so we downgrade two
# 3.1-specific constructs to their 3.0 equivalents on the way out:
#
#   1. The document `openapi` field is rewritten from "3.1.x" to
#      "3.0.3" — the version oapi-codegen targets.
#   2. `anyOf: [<type>, {"type": "null"}]` (FastAPI's encoding for
#      Optional[T]) is collapsed to `{<type>, "nullable": true}` —
#      the 3.0 idiom.
#
# Both transforms are lossless for v0.1's spec; if a richer 3.1
# construct ever lands in the backplane (`type: ["string","null"]`
# array form, prefixItems on tuples, etc.) extend this script
# alongside the change. The Makefile `snapshot-openapi` target
# drives this script; the resulting cli/api/openapi.json is the
# committed input to `make generate`.
#
# Run via `make snapshot-openapi` from the cli/ directory.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _downgrade_anyof_null(node: Any) -> Any:
    """Recursively rewrite OpenAPI 3.1 nullable anyOf patterns to 3.0 nullable."""
    if isinstance(node, dict):
        # anyOf collapse: remove {"type": "null"} branches and add nullable:true.
        #   [<schema>, {"type":"null"}]           → schema + nullable:true  (simple)
        #   [s1, s2, ..., {"type":"null"}]        → {anyOf:[s1,s2,...], nullable:true}
        any_of = node.get("anyOf")
        if isinstance(any_of, list):
            null_branches = [s for s in any_of if isinstance(s, dict) and s.get("type") == "null"]
            non_null = [s for s in any_of if not (isinstance(s, dict) and s.get("type") == "null")]
            if null_branches:
                sibling = {k: v for k, v in node.items() if k != "anyOf"}
                if len(non_null) == 1:
                    # Simple: collapse to single schema + nullable.
                    replacement = dict(non_null[0])
                    replacement["nullable"] = True
                    for key, value in sibling.items():
                        replacement.setdefault(key, value)
                else:
                    # Complex: keep anyOf without null, add nullable at parent.
                    replacement = {"anyOf": non_null, "nullable": True}
                    replacement.update(sibling)
                return _downgrade_anyof_null(replacement)
        return {k: _downgrade_anyof_null(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_downgrade_anyof_null(v) for v in node]
    return node


def downgrade(spec: dict) -> dict:
    """Apply all 3.1 → 3.0 transforms on a copy of the spec."""
    spec = json.loads(json.dumps(spec))  # deep copy
    spec["openapi"] = "3.0.3"
    spec = _downgrade_anyof_null(spec)
    return spec


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot meho backplane OpenAPI spec.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "openapi.json",
        help="Output path for the snapshot (default: cli/api/openapi.json).",
    )
    args = parser.parse_args()

    # Defer the FastAPI import until the script is actually run so the
    # cli/ module can be checked into the repo even when the backend's
    # uv environment isn't installed locally.
    try:
        from meho_backplane.main import app
    except ImportError as exc:
        print(
            "error: meho_backplane import failed. Run this script from the "
            "backend's uv env: `cd ../backend && uv run python ../cli/api/snapshot-openapi.py`",
            file=sys.stderr,
        )
        print(f"underlying error: {exc}", file=sys.stderr)
        return 2

    spec = app.openapi()
    downgraded = downgrade(spec)
    args.out.write_text(json.dumps(downgraded, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
