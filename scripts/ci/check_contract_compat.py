#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail when a change breaks the REST API, the MCP tools or the Helm values of MEHO.

Clients depend on committed snapshots and on the chart's values files:

* ``cli/api/openapi.json`` — the REST API. Only the public paths under
  ``/api/v1/`` carry the promise; the console paths under ``/ui/`` do not.
* ``backend/contracts/mcp-tools-list.json`` — the MCP tools: names, the
  surface they are listed on, and their input and output schemas.
* ``deploy/charts/meho/values.yaml`` and ``values.schema.json`` — the Helm
  values an operator sets.

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
  type, or a removed output schema. A schema without ``type`` accepts any
  type. ``oneOf`` and ``anyOf`` branches are paired by content, not by
  position: equal branches first, then the closest of the rest. A branch
  removed from an input or added to an output is breaking. In an input
  ``oneOf``, two branches that newly accept the same type are breaking too
  (a value that fits two branches fails ``oneOf``).
* Helm values: a key removed from ``values.yaml`` (an operator's
  ``--set`` or values file for it stops working), and in
  ``values.schema.json`` the same rules as for an MCP input schema (a
  removed property, a newly required property, a narrower type, a removed
  enum value).

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

CI: ``.github/workflows/compat-gates.yml``. Needs PyYAML; the
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

import yaml

OPENAPI_PATH = "cli/api/openapi.json"
MCP_PATH = "backend/contracts/mcp-tools-list.json"
PUBLIC_PATHS = "^/api/v1/"
VALUES_PATH = "deploy/charts/meho/values.yaml"
VALUES_SCHEMA_PATH = "deploy/charts/meho/values.schema.json"
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


_ANY_TYPE = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_BRANCH_KEYS = ("oneOf", "anyOf")


def _types(schema: dict[str, Any]) -> frozenset[str]:
    """Return the JSON types *schema* allows; no ``type`` key allows every type."""
    value = schema.get("type")
    if value is None:
        return _ANY_TYPE
    return frozenset({value} if isinstance(value, str) else value)


def _type_text(types: frozenset[str]) -> str:
    return "any" if types == _ANY_TYPE else str(sorted(types))


def _with_integer(types: frozenset[str]) -> frozenset[str]:
    """Return *types* plus ``integer`` when it has ``number``: an integer is a number."""
    return types | {"integer"} if "number" in types else types


def _compare_type(
    where: str, base: dict[str, Any], head: dict[str, Any], is_input: bool
) -> list[str]:
    old, new = _types(base), _types(head)
    if old == new or (is_input and old <= _with_integer(new)):
        return []
    return [f"{where}: type changed from {_type_text(old)} to {_type_text(new)}"]


def _branch_types(branch: Any) -> frozenset[str]:
    return _types(branch) if isinstance(branch, dict) else _ANY_TYPE


def _shared_types(first: frozenset[str], second: frozenset[str]) -> frozenset[str]:
    """Return the JSON types a value can have under both; an integer is a number."""
    return _with_integer(first) & _with_integer(second)


def _json_type(value: Any) -> str:
    """Return the JSON type name of an ``enum`` value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def _same_number(value: Any) -> Any:
    """Return *value* with every whole float as an int, the way JSON Schema compares.

    In JSON Schema ``1.0`` equals ``1`` and is an integer. A boolean is not
    a number, so ``true`` stays ``true`` and never equals ``1``.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_same_number(item) for item in value]
    if isinstance(value, dict):
        return {key: _same_number(item) for key, item in value.items()}
    return value


def _enum_text(value: Any) -> str:
    """Return the JSON text of an ``enum`` value; equal JSON Schema values get equal texts."""
    return json.dumps(_same_number(value), sort_keys=True)


def _enum_texts(branch: Any) -> set[str] | None:
    """Return the ``enum`` values of *branch* as JSON texts, or None without an ``enum``."""
    if not isinstance(branch, dict) or not isinstance(branch.get("enum"), list):
        return None
    return {_enum_text(value) for value in branch["enum"]}


def _accepted_by_both(first: Any, second: Any) -> frozenset[str]:
    """Return what a value can be to match both branches.

    An item is a JSON type name (every value of that type matches both) or
    the JSON text of an ``enum`` value, prefixed with ``=``. A branch with
    an ``enum`` accepts only those values, so two such branches share only
    the values both list, and an ``enum`` branch shares with a plain branch
    only the values of a type that the plain branch accepts.
    """
    shared = _shared_types(_branch_types(first), _branch_types(second))
    enums = [texts for texts in (_enum_texts(first), _enum_texts(second)) if texts is not None]
    if not enums:
        return shared
    values = set.intersection(*enums)
    return frozenset(f"={text}" for text in values if _json_type(json.loads(text)) in shared)


def _newly_accepted(before: frozenset[str], now: frozenset[str]) -> frozenset[str]:
    """Return the items of *now* that *before* did not cover.

    A type covers itself and every ``enum`` value of that type.
    """
    return frozenset(
        item
        for item in now
        if item not in before
        and not (item.startswith("=") and _json_type(json.loads(item[1:])) in before)
    )


def _accepted_text(items: frozenset[str]) -> str:
    types = frozenset(item for item in items if not item.startswith("="))
    if types:
        return _type_text(types)
    return "the values [" + ", ".join(sorted(item[1:] for item in items)) + "]"


def _pair_branches(old: list[Any], new: list[Any], is_input: bool) -> list[tuple[int, int]]:
    """Pair each old branch with one new branch, whatever their positions.

    Equal branches are paired first. Each old branch left over takes the
    new branch with the fewest differences (then the nearest position). A
    branch without a partner stays out of the list.
    """
    pairs: list[tuple[int, int]] = []
    free_new = list(range(len(new)))
    free_old: list[int] = []
    for index, branch in enumerate(old):
        equal = [j for j in free_new if new[j] == branch]
        if equal:
            pairs.append((index, equal[0]))
            free_new.remove(equal[0])
        else:
            free_old.append(index)
    for index in free_old:
        if not free_new:
            break
        closest = min(
            free_new,
            key=lambda j: (len(compare_schema("", old[index], new[j], is_input)), abs(index - j)),
        )
        pairs.append((index, closest))
        free_new.remove(closest)
    return sorted(pairs)


def _one_of_overlaps(
    where: str, old: list[Any], new: list[Any], pairs: list[tuple[int, int]]
) -> list[str]:
    """Report two input ``oneOf`` branches that newly accept the same values.

    A value must match exactly one ``oneOf`` branch. When a new branch, or a
    widened branch, accepts a value that another branch accepts too, that
    value matched before and is rejected now. Only what the two branches
    did not both accept before counts: an old overlap is not a new break.
    """
    partner = {j: i for i, j in pairs}
    found: list[str] = []
    for j in range(len(new)):
        for k in range(j + 1, len(new)):
            now = _accepted_by_both(new[j], new[k])
            before: frozenset[str] = frozenset()
            if j in partner and k in partner:
                before = _accepted_by_both(old[partner[j]], old[partner[k]])
            newly = _newly_accepted(before, now)
            if newly:
                found.append(
                    f"{where}: oneOf branches {j} and {k} both accept {_accepted_text(newly)} now "
                    "(a value that fits two branches fails oneOf)"
                )
    return found


def _branch_list_added_or_removed(
    where: str, key: str, old: Any, new: Any, is_input: bool
) -> list[str]:
    """Report a ``oneOf``/``anyOf`` list that only *base* or only *head* has."""
    if isinstance(old, list) and not is_input:
        return [f"{where}: {key} removed"]
    if isinstance(new, list) and is_input:
        return [f"{where}: {key} added"]
    return []


def _compare_branch_lists(
    where: str, key: str, old: list[Any], new: list[Any], is_input: bool
) -> list[str]:
    found: list[str] = []
    fewer_or_more = len(new) < len(old) if is_input else len(new) > len(old)
    if fewer_or_more:
        found.append(f"{where}: {key} has {len(new)} branches instead of {len(old)}")
    pairs = _pair_branches(old, new, is_input)
    for index, partner in pairs:
        found += compare_schema(f"{where} {key}[{partner}]", old[index], new[partner], is_input)
    if is_input and key == "oneOf":
        found += _one_of_overlaps(where, old, new, pairs)
    return found


def _compare_branches(
    where: str, base: dict[str, Any], head: dict[str, Any], is_input: bool
) -> list[str]:
    """Compare ``oneOf`` and ``anyOf`` branches; the index is the position in *head*."""
    found: list[str] = []
    for key in _BRANCH_KEYS:
        old, new = base.get(key), head.get(key)
        if isinstance(old, list) and isinstance(new, list):
            found += _compare_branch_lists(where, key, old, new, is_input)
        else:
            found += _branch_list_added_or_removed(where, key, old, new, is_input)
    return found


def _compare_enum(
    where: str, base: dict[str, Any], head: dict[str, Any], is_input: bool
) -> list[str]:
    if not is_input or "enum" not in base:
        return []
    if "enum" not in head:
        return []
    kept = {_enum_text(value) for value in head["enum"]}
    removed = [value for value in base["enum"] if _enum_text(value) not in kept]
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
    found += _compare_branches(where, base, head, is_input)
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
# Helm values
# ---------------------------------------------------------------------------


def _key_paths(node: Any, prefix: str = "") -> set[str]:
    """Return the dotted path of every key in a nested mapping (lists are leaves)."""
    paths: set[str] = set()
    if not isinstance(node, dict):
        return paths
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.add(path)
        paths |= _key_paths(value, path)
    return paths


def values_breaking(base_text: str, head_text: str) -> list[str]:
    """List the keys that ``values.yaml`` had at the base and no longer has."""
    base_keys = _key_paths(yaml.safe_load(base_text))
    head_keys = _key_paths(yaml.safe_load(head_text))
    return [f"Helm value {path!r} removed" for path in sorted(base_keys - head_keys)]


def _resolve_local(document: Any, ref: str) -> Any:
    """Return the part of *document* that the local reference *ref* points to."""
    node = document
    for part in ref[2:].split("/") if ref != "#" else []:
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise CheckError(f"$ref {ref!r} points to nothing")
        node = node[part]
    return node


def inline_refs(document: Any) -> Any:
    """Return *document* with every local ``$ref`` replaced.

    A ``{"$ref": "#/definitions/x"}`` becomes a copy of ``definitions.x``, so
    a change inside a definition is compared at every place that uses it.
    Keys next to ``$ref`` are kept. A reference that points back into its own
    chain is left as it is, so a self-referencing schema cannot loop.
    """
    return _inline(document, document, ())


def _inline(document: Any, node: Any, chain: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(document, item, chain) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#") and ref not in chain:
        target = _inline(document, _resolve_local(document, ref), (*chain, ref))
        rest = {k: _inline(document, v, chain) for k, v in node.items() if k != "$ref"}
        return {**target, **rest} if isinstance(target, dict) else target
    return {key: _inline(document, value, chain) for key, value in node.items()}


def values_schema_breaking(base: Any, head: Any) -> list[str]:
    """List the breaking changes between two ``values.schema.json`` files."""
    return compare_schema("Helm values schema", inline_refs(base), inline_refs(head), True)


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
    found += _helm_breaking(root, base_ref)
    return found


def _helm_breaking(root: pathlib.Path, base_ref: str) -> list[str]:
    found: list[str] = []
    base_values = read_at(root, base_ref, VALUES_PATH)
    if base_values is None:
        print(f"note: {VALUES_PATH} does not exist at {base_ref}; Helm values check skipped")
    else:
        head_values = (root / VALUES_PATH).read_text(encoding="utf-8")
        found += values_breaking(base_values, head_values)
    base_schema = read_at(root, base_ref, VALUES_SCHEMA_PATH)
    if base_schema is None:
        print(f"note: {VALUES_SCHEMA_PATH} does not exist at {base_ref}; Helm schema check skipped")
    else:
        head_schema = (root / VALUES_SCHEMA_PATH).read_text(encoding="utf-8")
        found += values_schema_breaking(json.loads(base_schema), json.loads(head_schema))
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
    except (CheckError, json.JSONDecodeError, yaml.YAMLError, OSError) as exc:
        print(f"::error title=Compatibility check could not run::{exc}")
        return 2

    if not found:
        print(f"No breaking change in the REST API, the MCP tools or the Helm values since {args.base}.")
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
        "::error title=Breaking change::This change breaks clients of the REST API, the "
        f"MCP tools or the Helm values. It needs {missing}. In that section, write what a client must change. "
        "Or change the code so that it stays compatible."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
