#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when a change breaks the REST API or the MCP tools of MEHO.

Clients depend on two committed snapshots:

* ``cli/api/openapi.json`` — the REST API. Only the public paths under
  ``/api/v1/`` carry the promise; the console paths under ``/ui/`` do not.
* ``backend/contracts/mcp-tools-list.json`` — the MCP tools: names, the
  surface they are listed on, and their input and output schemas.

Other checks keep both files equal to the code (the CI job
``cli-api-snapshot-freshness`` and ``tests/test_mcp_tools_snapshot_drift.py``).
This script compares each file at ``--base`` with the file in the working
tree and lists every breaking change:

* REST API: ``oasdiff breaking`` with ``--fail-on ERR`` (its definite
  breaking changes, such as a removed path, a removed response field or a
  new required parameter).
* MCP tools: a removed tool; a tool moved from the working surface to the
  operator surface; in an input schema a removed argument, a newly
  required argument, a narrower type or a removed enum value; in an output
  schema a removed field, a field that is no longer required, a changed
  type, or a removed output schema.

A breaking change is allowed only on a pull request that has the
``breaking-change`` label **and** a ``## Breaking changes`` section in its
text, with the steps a client must take (the release pull request copies
it into the CHANGELOG). When the file does not exist at ``--base`` yet,
its check is skipped with a note.

Exit codes: 0 no breaking change (or an allowed one), 1 a breaking change
that is not allowed, 2 the check could not run.

Usage::

    python scripts/ci/check_contract_compat.py --base HEAD^1 \\
        --labels "breaking-change,docs" --pr-body-file body.md

CI: ``.github/workflows/compat-gates.yml``. Standard library only; the
REST check needs the ``oasdiff`` binary (``--oasdiff``).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Any

OPENAPI_PATH = "cli/api/openapi.json"
MCP_PATH = "backend/contracts/mcp-tools-list.json"
PUBLIC_PATHS = "^/api/v1/"
LABEL = "breaking-change"

_BREAKING_HEADING = re.compile(r"^#{2,6}\s*Breaking changes\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,6}\s")


class CheckError(Exception):
    """The check could not run (exit code 2)."""


# ---------------------------------------------------------------------------
# Reading the files
# ---------------------------------------------------------------------------


def read_at(root: pathlib.Path, ref: str, path: str) -> str | None:
    """Return the text of *path* at git *ref*, or ``None`` when it is not there."""
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{ref}:{path}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if verify.returncode != 0:
            raise CheckError(f"git ref {ref!r} does not exist (fetch more history?)")
        return None
    shown = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        raise CheckError(f"git show {ref}:{path} failed: {shown.stderr.strip()}")
    return shown.stdout


# ---------------------------------------------------------------------------
# REST API (oasdiff)
# ---------------------------------------------------------------------------


def openapi_breaking(oasdiff: str, base_file: pathlib.Path, head_file: pathlib.Path) -> list[str]:
    """Return the breaking changes ``oasdiff`` finds on the public paths."""
    cmd = [
        oasdiff,
        "breaking",
        "--fail-on",
        "ERR",
        "--match-path",
        PUBLIC_PATHS,
        "--format",
        "singleline",
        str(base_file),
        str(head_file),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise CheckError(f"cannot run oasdiff ({oasdiff}): {exc}") from exc
    if result.returncode == 0:
        return []
    if result.returncode == 1:
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        errors = [line for line in lines if line.lower().startswith("error")]
        return [f"REST API: {line}" for line in errors or lines] or [
            "REST API: oasdiff found a breaking change (no text)"
        ]
    raise CheckError(
        f"oasdiff exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
    )


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _types(schema: dict[str, Any]) -> set[str] | None:
    value = schema.get("type")
    if value is None:
        return None
    return {value} if isinstance(value, str) else set(value)


def _compare_type(
    where: str, base: dict[str, Any], head: dict[str, Any], is_input: bool
) -> list[str]:
    old, new = _types(base), _types(head)
    if old is None or new is None or old == new:
        return []
    if is_input and old <= new:
        return []
    return [f"{where}: type changed from {sorted(old)} to {sorted(new)}"]


def _compare_enum(
    where: str, base: dict[str, Any], head: dict[str, Any], is_input: bool
) -> list[str]:
    if not is_input or "enum" not in base:
        return []
    if "enum" not in head:
        return []
    removed = [value for value in base["enum"] if value not in head["enum"]]
    return [f"{where}: enum value {value!r} removed" for value in removed]


def _compare_required(
    where: str, base: dict[str, Any], head: dict[str, Any], is_input: bool
) -> list[str]:
    old, new = set(base.get("required", [])), set(head.get("required", []))
    if is_input:
        return [f"{where}: argument {name!r} is now required" for name in sorted(new - old)]
    return [f"{where}: field {name!r} is no longer always returned" for name in sorted(old - new)]


def compare_schema(where: str, base: Any, head: Any, is_input: bool) -> list[str]:
    """List the breaking changes from schema *base* to schema *head*.

    *is_input* is true for a schema the client sends (tool arguments) and
    false for a schema the client reads (the structured result).
    """
    if not isinstance(base, dict) or not isinstance(head, dict):
        return []
    found = _compare_type(where, base, head, is_input)
    found += _compare_enum(where, base, head, is_input)
    found += _compare_required(where, base, head, is_input)
    old_props = base.get("properties", {})
    new_props = head.get("properties", {})
    kind = "argument" if is_input else "field"
    for name in sorted(old_props):
        if name not in new_props:
            found.append(f"{where}: {kind} {name!r} removed")
            continue
        found += compare_schema(f"{where}.{name}", old_props[name], new_props[name], is_input)
    if "items" in base and "items" in head:
        found += compare_schema(f"{where}[]", base["items"], head["items"], is_input)
    return found


def _compare_tool(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    name = base["name"]
    found: list[str] = []
    if base.get("surface") == "working" and head.get("surface") != "working":
        found.append(f"MCP tool {name}: moved off the working surface to {head.get('surface')!r}")
    found += compare_schema(
        f"MCP tool {name} input", base["inputSchema"], head["inputSchema"], True
    )
    old_out, new_out = base.get("outputSchema"), head.get("outputSchema")
    if old_out is not None and new_out is None:
        found.append(f"MCP tool {name}: output schema removed")
    else:
        found += compare_schema(f"MCP tool {name} output", old_out, new_out, False)
    return found


def mcp_breaking(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    """List the breaking changes between two MCP tool snapshots."""
    new_tools = {tool["name"]: tool for tool in head.get("tools", [])}
    found: list[str] = []
    for tool in base.get("tools", []):
        if tool["name"] not in new_tools:
            found.append(f"MCP tool {tool['name']}: removed")
            continue
        found += _compare_tool(tool, new_tools[tool["name"]])
    return found


# ---------------------------------------------------------------------------
# Is the break allowed?
# ---------------------------------------------------------------------------


def has_breaking_section(body: str) -> bool:
    """True when *body* has a ``## Breaking changes`` heading with text under it."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if not _BREAKING_HEADING.match(line.strip()):
            continue
        for following in lines[index + 1 :]:
            text = following.strip()
            if _ANY_HEADING.match(text):
                break
            if text and not text.startswith("<!--"):
                return True
    return False


def allowed(labels: list[str], body: str | None) -> tuple[bool, str]:
    """Decide whether a breaking change may merge, and say why not."""
    missing = []
    if LABEL not in labels:
        missing.append(f"the '{LABEL}' label")
    if body is None or not has_breaking_section(body):
        missing.append("a '## Breaking changes' section in the pull request text")
    if missing:
        return False, " and ".join(missing)
    return True, ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def collect(root: pathlib.Path, base_ref: str, oasdiff: str) -> list[str]:
    """Run both checks and return every breaking change."""
    found: list[str] = []
    base_openapi = read_at(root, base_ref, OPENAPI_PATH)
    if base_openapi is None:
        print(f"note: {OPENAPI_PATH} does not exist at {base_ref}; REST API check skipped")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            base_file = pathlib.Path(tmp) / "base-openapi.json"
            base_file.write_text(base_openapi, encoding="utf-8")
            found += openapi_breaking(oasdiff, base_file, root / OPENAPI_PATH)
    base_mcp = read_at(root, base_ref, MCP_PATH)
    if base_mcp is None:
        print(f"note: {MCP_PATH} does not exist at {base_ref}; MCP tools check skipped")
    else:
        head_mcp = (root / MCP_PATH).read_text(encoding="utf-8")
        found += mcp_breaking(json.loads(base_mcp), json.loads(head_mcp))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="git ref to compare with")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--oasdiff", default="oasdiff", help="path of the oasdiff binary")
    parser.add_argument("--labels", default=None, help="comma-separated pull request labels")
    parser.add_argument("--pr-body-file", default=None, help="file with the pull request text")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    try:
        found = collect(root, args.base, args.oasdiff)
    except (CheckError, json.JSONDecodeError, OSError) as exc:
        print(f"::error title=Compatibility check could not run::{exc}")
        return 2

    if not found:
        print(f"No breaking change in the REST API or the MCP tools since {args.base}.")
        return 0
    print(f"{len(found)} breaking change(s) since {args.base}:")
    for line in found:
        print(f"  - {line}")

    labels = [label.strip() for label in (args.labels or "").split(",") if label.strip()]
    body = None
    if args.pr_body_file:
        body = pathlib.Path(args.pr_body_file).read_text(encoding="utf-8")
    ok, missing = allowed(labels, body)
    if ok:
        print(
            f"Allowed: the pull request has the '{LABEL}' label and a 'Breaking changes' section."
        )
        return 0
    print(
        "::error title=Breaking change::This change breaks clients of the REST API or the "
        f"MCP tools. It needs {missing}. In that section, write what a client must change. "
        "Or change the code so that it stays compatible."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
