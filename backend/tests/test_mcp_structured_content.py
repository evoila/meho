# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Dispatcher-level guards for MCP ``structuredContent`` emission (#2774).

MCP 2025-06-18 §Tools/Output Schema: a tool that declares an
``outputSchema`` **MUST** return structured results that conform to it,
and SHOULD keep the serialised JSON in a ``TextContent`` block for
backwards compatibility. Claude's frontend enforces the MUST client-side
— before #2774 every declaring tool hard-failed in Claude Desktop with
"Tool execution failed" while the backplane logged 200 and wrote its
audit row (the third Anthropic-conformance class after #2644's
schema-shape 400s and #2745's tool-name-pattern rejection).

Three guard families live here:

* **Emission** — ``handle_tools_call`` packs the handler's dict as
  ``structuredContent`` alongside the text block for a declaring tool,
  with the two byte-identical; a tool without an ``outputSchema`` gets
  no ``structuredContent`` key (nothing to conform to, no wire bloat).
* **Schema well-formedness** — every declared ``outputSchema`` is a
  valid JSON Schema 2020-12 document whose root is ``type: object``
  (``structuredContent`` is defined as a JSON object, so a non-object
  root could never validate any result).
* **Declaring-set pin** — the exact set of declaring tools is pinned so
  a new declaration cannot land without a per-tool payload-conformance
  scenario in ``tests/test_mcp_output_schema_conformance.py`` (the
  companion file that validates real handler results against the
  declared schemas).
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.registry import ToolDefinition
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

#: Every tool on the registry that declares an ``outputSchema``. Pinned
#: exactly: adding a declaration without extending this set (AND adding a
#: payload-conformance scenario in
#: ``tests/test_mcp_output_schema_conformance.py``) fails
#: ``test_declaring_tool_set_is_pinned`` with instructions. The issue
#: (#2774) counted 12 on v0.27.0; five operation/JSONFlux tools declared
#: schemas between v0.27.0 and this pin.
EXPECTED_DECLARING_TOOLS: frozenset[str] = frozenset(
    {
        "call_operation",
        "create_doc_collections",
        "delete_doc_collections",
        "list_doc_collections",
        "list_operation_groups",
        "list_targets",
        "meho_audit_replay",
        "meho_topology_annotate",
        "meho_topology_bulk_import",
        "meho_topology_create_node",
        "meho_topology_delete_node",
        "meho_topology_unannotate",
        "preview_operation",
        "query_audit",
        "query_topology",
        "result_query",
        "search_operations",
    }
)


def _declaring_tools() -> dict[str, ToolDefinition]:
    """Every registered tool with a non-None ``outputSchema``, by name."""
    return {
        defn.name: defn
        for defn, _handler in mcp_registry._TOOLS.values()
        if defn.outputSchema is not None
    }


# ---------------------------------------------------------------------------
# Declaring-set pin + schema well-formedness
# ---------------------------------------------------------------------------


def test_declaring_tool_set_is_pinned() -> None:
    """The set of outputSchema-declaring tools matches the pinned roster.

    A new declaration is a new client-enforced contract (MCP 2025-06-18:
    declaring tools MUST return conforming structured results). Landing
    one requires (a) extending :data:`EXPECTED_DECLARING_TOOLS` and (b)
    adding a payload-conformance scenario to
    ``tests/test_mcp_output_schema_conformance.py`` so the contract is
    machine-checked against a real handler result before a conforming
    client (Claude Desktop) is the first thing to validate it.
    """
    declaring = set(_declaring_tools())
    unexpected = declaring - EXPECTED_DECLARING_TOOLS
    missing = EXPECTED_DECLARING_TOOLS - declaring
    assert not unexpected, (
        f"new outputSchema declaration(s) {sorted(unexpected)}: add each to "
        "EXPECTED_DECLARING_TOOLS and add a payload-conformance scenario in "
        "tests/test_mcp_output_schema_conformance.py"
    )
    assert not missing, (
        f"tool(s) {sorted(missing)} dropped their outputSchema declaration: "
        "remove them from EXPECTED_DECLARING_TOOLS and from the conformance "
        "scenarios (dropping a declaration is spec-legal — see #2774)"
    )


def test_every_declared_output_schema_is_valid_2020_12() -> None:
    """Every declared ``outputSchema`` is a well-formed 2020-12 schema.

    ``jsonschema.validate`` at test time (and any client-side validator)
    would raise ``SchemaError`` on a malformed document — which reads as
    a tool failure, not a schema bug. Check the schemas themselves.
    """
    for name, defn in sorted(_declaring_tools().items()):
        schema = defn.outputSchema
        assert schema is not None
        jsonschema.Draft202012Validator.check_schema(schema)
        # MCP §Tools/Structured Content: structuredContent is a JSON
        # *object*. A non-object root could never validate any result.
        assert schema.get("type") == "object", (
            f"{name}: outputSchema root must be type 'object', got {schema.get('type')!r}"
        )


def test_output_schema_survives_to_wire() -> None:
    """``to_wire`` publishes the declared ``outputSchema`` verbatim.

    The client-side validation contract only exists because the schema
    is published on ``tools/list``; a wire-shape regression that drops
    or mutates it would silently disarm every conforming client's
    validation (and orphan the structuredContent this suite pins).
    """
    for name, defn in sorted(_declaring_tools().items()):
        wire = defn.to_wire()
        assert wire.get("outputSchema") == defn.outputSchema, (
            f"{name}: to_wire() must publish outputSchema verbatim"
        )


# ---------------------------------------------------------------------------
# Dispatcher emission
# ---------------------------------------------------------------------------


def _tools_call(
    client: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    req_id: int = 1,
) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_declaring_tool_result_carries_matching_structured_content(
    client_with_operator: Any,  # noqa: F811 — pytest indirect fixture
) -> None:
    """A declaring tool's result carries ``structuredContent`` == text block.

    ``meho_topology_create_node`` is the cheapest declaring tool (no
    seeding required — the node insert is the operation). The spec's
    backwards-compatibility recommendation makes the text block and the
    structured object the same document; byte-identical equality is the
    strictest honest pin.
    """
    client, _op = client_with_operator
    response = _tools_call(
        client,
        "meho_topology_create_node",
        {"kind": "vault-role", "name": "structured-content-pin"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    result = body["result"]
    assert result["isError"] is False
    assert "structuredContent" in result, (
        "outputSchema-declaring tool returned no structuredContent — "
        "hard-fails every conforming MCP client (#2774)"
    )
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])


def test_non_declaring_tool_result_has_no_structured_content(
    client_with_operator: Any,  # noqa: F811 — pytest indirect fixture
) -> None:
    """A tool without an ``outputSchema`` gets no ``structuredContent`` key.

    Emission is gated on the declaration: doubling every payload for
    the ~60 non-declaring tools would bloat the wire for no contract
    gain, and ``meho_status`` (no declaration) is the proven-working
    Desktop round-trip shape the #2666 smoke run measured.
    """
    client, _op = client_with_operator
    response = _tools_call(client, "meho_status", {})
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    result = body["result"]
    assert result["isError"] is False
    assert "structuredContent" not in result
