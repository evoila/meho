#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the committed snapshot of the MCP ``tools/list`` contract.

The snapshot ``backend/contracts/mcp-tools-list.json`` holds, for every
registered MCP tool, the parts that clients depend on:

* ``name`` — the tool name;
* ``surface`` — ``working`` (every session lists it) or ``operator``
  (only a session with the ``mcp:admin`` scope lists it);
* ``inputSchema`` — the schema that ``tools/list`` sends (after
  :meth:`~meho_backplane.mcp.registry.ToolDefinition.to_wire`);
* ``outputSchema`` — the schema of the structured result, or ``null``.

Text that helps a reader but is not a contract (``description``,
``title``, ``examples``, ``$comment``) is left out of the schemas, so a
wording change does not touch the snapshot.

Two checks use the file:

* ``tests/test_mcp_tools_snapshot_drift.py`` fails when the committed
  file and the registry disagree (run this script and commit the result);
* ``scripts/ci/check_contract_compat.py`` (repo root) compares the file
  with the one on the base branch and fails on a breaking change.

Run from ``backend/``::

    uv run python scripts/snapshot_mcp_tools.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from meho_backplane.mcp.registry import eager_import_mcp_modules

_BACKEND_DIR = Path(__file__).resolve().parents[1]

#: Where the snapshot lives, relative to ``backend/``.
SNAPSHOT_RELPATH = "contracts/mcp-tools-list.json"

#: Schema keywords that carry reader text only.
_TEXT_KEYWORDS = frozenset({"description", "title", "examples", "$comment"})

#: Keywords whose value is a map of name -> sub-schema.
_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions")

#: Keywords whose value is one sub-schema.
_SCHEMA_ONE = ("items", "additionalProperties", "not", "contains", "if", "then", "else")

#: Keywords whose value is a list of sub-schemas.
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")


def strip_text(schema: Any) -> Any:
    """Return *schema* without its reader-text keywords, at every depth.

    Only schema keywords are dropped. A property that is *named*
    ``description`` stays, because the names under ``properties`` are
    data, not keywords.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _TEXT_KEYWORDS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: strip_text(sub) for name, sub in value.items()}
        elif key in _SCHEMA_ONE:
            out[key] = strip_text(value)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [strip_text(sub) for sub in value]
        else:
            out[key] = value
    return out


def build_snapshot() -> dict[str, Any]:
    """Build the snapshot from the in-process MCP tool registry."""
    eager_import_mcp_modules()
    from meho_backplane.mcp.registry import _TOOLS

    tools = []
    for defn, _handler in sorted(_TOOLS.values(), key=lambda item: item[0].name):
        wire = defn.to_wire()
        output = wire.get("outputSchema")
        tools.append(
            {
                "name": defn.name,
                "surface": defn.surface.value,
                "inputSchema": strip_text(wire["inputSchema"]),
                "outputSchema": strip_text(output) if output is not None else None,
            }
        )
    return {"tools": tools}


def render_snapshot() -> str:
    """Render the snapshot as stable, sorted JSON text."""
    return json.dumps(build_snapshot(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    """Write the snapshot file."""
    target = _BACKEND_DIR / SNAPSHOT_RELPATH
    target.write_text(render_snapshot(), encoding="utf-8")
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
