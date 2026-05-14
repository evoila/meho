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
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationGroup
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
    """``tools/call list_operation_groups`` round-trips through the handler."""
    client, op = client_with_operator

    # Seed a built-in group so the handler has something to return.
    # The TestClient enters the FastAPI lifespan (the fixture uses
    # `with TestClient(app)`), so the DB is migrated and ready.
    async def _seed() -> None:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as s, s.begin():
            s.add(
                OperationGroup(
                    id=uuid.uuid4(),
                    tenant_id=None,
                    product="vault",
                    version="1.x",
                    impl_id="vault",
                    group_key="kv",
                    name="KV v2",
                    when_to_use="Use for reading and writing KV v2 secrets.",
                    review_status="enabled",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )

    import asyncio

    asyncio.run(_seed())

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
