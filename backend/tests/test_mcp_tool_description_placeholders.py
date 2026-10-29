# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tool descriptions must not hold values a model copies as real choices (#4111).

Small models copy example values out of a tool description. Two cases were
seen: a ``status`` filter copied from ``meho_connector_list``, and a real
target name copied from ``call_operation`` / ``preview_operation``. These
tests read the text the tools send to a client (``ToolDefinition.to_wire()``)
and check that:

* the ``target`` examples use the ``<target-name>`` placeholder and say
  where a real name comes from (``list_targets``);
* ``meho_connector_list`` gives no ``status=`` example and names no tool
  that a default session cannot see.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface
from tests.mcp_test_fixtures import (
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_TARGET_TOOLS = ("call_operation", "preview_operation")


def _definition(name: str) -> ToolDefinition:
    entry = mcp_registry.get_tool(name)
    assert entry is not None, name
    return entry[0]


def _wire(name: str) -> dict[str, Any]:
    return _definition(name).to_wire()


@pytest.mark.parametrize("tool", _TARGET_TOOLS)
def test_target_examples_use_a_placeholder(tool: str) -> None:
    wire = _wire(tool)
    target_desc = wire["inputSchema"]["properties"]["target"]["description"]
    for text in (wire["description"], target_desc):
        assert '"<target-name>"' in text
        assert '{"name": "<target-name>"}' in text
        assert "list_targets" in text
    # No example target name other than the placeholder anywhere in the
    # text the tool sends.
    examples = re.findall(r'\{"name": "([^"]+)"\}', json.dumps(wire).replace('\\"', '"'))
    assert examples
    assert set(examples) == {"<target-name>"}


def test_connector_list_suggests_no_status_value() -> None:
    wire = _wire("meho_connector_list")
    assert "status=" not in json.dumps(wire)


def test_connector_list_names_only_default_session_tools() -> None:
    description = _wire("meho_connector_list")["description"]
    mentioned = {
        name
        for name in mcp_registry.registered_tool_names()
        if re.search(rf"\b{name}\b", description)
    }
    hidden = {name for name in mentioned if _definition(name).surface is not ToolSurface.WORKING}
    assert hidden == set()
