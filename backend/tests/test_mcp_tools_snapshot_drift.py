# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Freshness gate for the committed MCP ``tools/list`` snapshot.

``backend/contracts/mcp-tools-list.json`` is written by
``backend/scripts/snapshot_mcp_tools.py`` from the in-process tool
registry. The compatibility gate (``scripts/ci/check_contract_compat.py``,
CI workflow ``compat-gates.yml``) compares it with the base branch. That
only works when the committed file matches the code, which this test
checks.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.snapshot_mcp_tools import SNAPSHOT_RELPATH, render_snapshot, strip_text

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_snapshot_is_fresh() -> None:
    committed = (_BACKEND_DIR / SNAPSHOT_RELPATH).read_text(encoding="utf-8")
    assert committed == render_snapshot(), (
        f"{SNAPSHOT_RELPATH} is out of date. Run "
        "'cd backend && uv run python scripts/snapshot_mcp_tools.py' and commit the result."
    )


def test_snapshot_lists_every_tool_once_with_a_surface() -> None:
    tools = json.loads(render_snapshot())["tools"]
    names = [tool["name"] for tool in tools]
    assert names == sorted(set(names))
    assert {tool["surface"] for tool in tools} <= {"working", "operator"}
    assert all(tool["inputSchema"].get("type") == "object" for tool in tools)


def test_strip_text_drops_reader_text_but_keeps_a_property_named_description() -> None:
    schema = {
        "type": "object",
        "description": "drop me",
        "title": "drop me",
        "properties": {
            "description": {"type": "string", "description": "drop me"},
            "tags": {"type": "array", "items": {"type": "string", "examples": ["x"]}},
            "choice": {"anyOf": [{"type": "string", "$comment": "x"}, {"type": "null"}]},
        },
        "additionalProperties": {"type": "integer", "title": "drop me"},
        "required": ["description"],
    }
    assert strip_text(schema) == {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "choice": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "additionalProperties": {"type": "integer"},
        "required": ["description"],
    }


def test_strip_text_leaves_non_schema_values_alone() -> None:
    assert strip_text(True) is True
    assert strip_text({"items": False, "enum": ["description"]}) == {
        "items": False,
        "enum": ["description"],
    }
