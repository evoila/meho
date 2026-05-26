# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the three operation MCP tools (G0.6-T8, #399).

Covers acceptance criteria 4 + 7 on issue #399:

* ``tools/list`` from a fresh MCP client surfaces three operation-family
  tools: ``list_operation_groups``, ``search_operations``,
  ``call_operation``.
* Each tool's ``inputSchema`` is JSON-Schema-2020-12 well-formed with
  ``additionalProperties: false``.
* MEHO-internal fields (``required_role``, ``op_class``) are stripped
  from the wire shape (same contract as the meho.status reference tool).
* ``tools/call list_operation_groups`` against a seeded group returns the
  groups list with the agent-facing ``when_to_use`` blurb.

The fixture operator is parameterised to :class:`TenantRole.OPERATOR`
(not the default READ_ONLY) because all three operation tools require
operator-or-above to pass the registry's RBAC list-time filter.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_exposes_three_operation_tools(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``tools/list`` includes the three operation meta-tools."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    body = response.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "list_operation_groups" in names
    assert "search_operations" in names
    assert "call_operation" in names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_input_schemas_are_strict_2020_12(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Each tool's inputSchema is well-formed and rejects unknown fields."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}

    for tool_name in (
        "list_operation_groups",
        "search_operations",
        "call_operation",
    ):
        schema = tools[tool_name]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        # Connector id is required on every operation tool.
        assert "connector_id" in schema["properties"]
        assert "connector_id" in schema["required"]
        # MEHO-internal fields are stripped from the wire shape.
        assert "required_role" not in tools[tool_name]
        assert "op_class" not in tools[tool_name]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_descriptions_include_when_to_use_guidance(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Descriptions name when to call and when NOT to call -- AI best-practices anchor."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}

    # The descriptions are load-bearing for agent UX; assert they are
    # non-trivial and mention the discovery / dispatch flow.
    list_groups_desc = tools["list_operation_groups"]["description"].lower()
    assert "group" in list_groups_desc
    assert "when_to_use" in list_groups_desc or "when to use" in list_groups_desc

    search_desc = tools["search_operations"]["description"].lower()
    assert "search" in search_desc or "retrieval" in search_desc

    call_desc = tools["call_operation"]["description"].lower()
    assert "search_operations" in call_desc  # the recipe nudge


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_list_operation_groups_returns_seeded_groups(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``tools/call list_operation_groups`` round-trips through the handler.

    Post-G0.6-T-Refactor-Vault, the FastAPI lifespan runs
    :func:`~meho_backplane.operations.run_typed_op_registrars` which
    calls :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
    and creates an :class:`OperationGroup` row for
    ``(product="vault", version="1.x", impl_id="vault",
    group_key="kv")`` as a side-effect of registering the
    ``vault.kv.read`` op. The pre-refactor variant of this test
    inserted its own ``OperationGroup`` row to give the handler
    something to enumerate; that insert now races with the
    lifespan's own and trips the partial-unique constraint. We rely
    on the lifespan-created group instead — the test's contract
    (the handler returns a group for ``connector_id="vault-1.x"``)
    is unchanged.
    """
    client, op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "list_operation_groups",
                "arguments": {"connector_id": "vault-1.x"},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["connector_id"] == "vault-1.x"
    assert any(g["group_key"] == "kv" for g in payload["groups"])
    # Sanity: the operator's tenant_id from the fixture matches the
    # tool's view (built-in rows are visible to everyone regardless).
    assert op.tenant_id == OPERATOR_TENANT_ID


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_search_operations_rejects_missing_query(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``search_operations`` requires ``query`` per its inputSchema."""
    from meho_backplane.mcp.schemas import INVALID_PARAMS

    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "search_operations",
                "arguments": {"connector_id": "vault-1.x"},  # missing query
            },
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_call_operation_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``additionalProperties: false`` blocks unknown fields on ``call_operation``."""
    from meho_backplane.mcp.schemas import INVALID_PARAMS

    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {
                    "connector_id": "vault-1.x",
                    "op_id": "vault.kv.read",
                    "unknown_field": "should be rejected",
                },
            },
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_call_operation_input_schema_accepts_both_target_shapes(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """G0.13-T2 #1132: the ``target`` JSON Schema accepts string + object + null.

    The published ``inputSchema`` for ``call_operation`` is the contract
    every MCP client validates against. The G0.13-T2 additive widening
    must surface in the wire schema (bare string is a valid value) so
    consumers see the contract from ``tools/list`` without trial and
    error. Asserts the wire shape directly -- the schema enum on
    ``target.type`` must be ``["string", "object", "null"]`` in that
    order (or any order; we sort before comparing).
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    target_schema = tools["call_operation"]["inputSchema"]["properties"]["target"]
    assert sorted(target_schema["type"]) == sorted(["string", "object", "null"])
    # The dict-shape ``name`` / ``fqdn`` inner properties stay declared
    # so dict callers still see the inner contract.
    assert "name" in target_schema["properties"]
    assert "fqdn" in target_schema["properties"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_call_operation_accepts_bare_string_target(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """G0.13-T2 #1132: schema-level acceptance of bare-string ``target``.

    Asserts the MCP dispatcher's JSON-Schema 2020-12 validation does
    NOT reject the bare-string ``target`` shape with a ``-32602``
    invalid_params error. The
    :func:`test_call_operation_input_schema_accepts_both_target_shapes`
    test pins the union shape on the wire schema; this test pins the
    runtime side -- a real ``tools/call`` with a bare-string target
    is not rejected pre-handler.

    The full dispatch path (target resolution, registry lookup, op
    invocation) is not exercised here; those happen further downstream
    and depend on per-fixture target / connector seeding. The
    behavioural round-trip for both shapes lives in
    :mod:`tests.test_operations_meta_tools` (the
    ``test_call_operation_with_bare_string_target_resolves_and_dispatches``
    test against the same in-process handler the MCP dispatcher calls).
    """
    from meho_backplane.mcp.schemas import INVALID_PARAMS

    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {
                    # Bare-string ``target`` is the contract under
                    # test. ``op_id`` is deliberately fake; we only
                    # assert "the schema layer accepted bare-string",
                    # not that the dispatch completed cleanly.
                    "connector_id": "vault-1.x",
                    "op_id": "vault.does.not.exist",
                    "target": "rdc-vault",
                },
            },
        },
    )
    body = response.json()
    if "error" in body:
        # Other failure shapes (internal-error from the resolver, etc.)
        # are fine here; the contract under test is "the schema did not
        # reject pre-handler with INVALID_PARAMS".
        assert body["error"]["code"] != INVALID_PARAMS, body
