# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G0.9.1-T6 ``meho.topology.create_node`` admin tool.

Coverage matrix (Task #778 acceptance criteria — the MCP-level half;
the substrate is covered in :mod:`tests.test_topology_create_node`):

* The tool registers with ``required_role=TENANT_ADMIN`` and
  ``op_class='write'``; non-admin sessions do not see it in
  ``tools/list`` and a direct ``tools/call`` from an operator returns
  -32602 ``forbidden``.
* ``tools/call meho.topology.create_node {kind, name}`` creates a
  fresh ``graph_node`` row in the operator's tenant and returns
  ``{node_id, kind, name, source: "curated", was_created: true}``.
* A repeat ``tools/call`` is idempotent — returns ``was_created:
  false`` and refreshes the existing row's ``last_seen`` + manual-seed
  properties without duplicating.
* A non-vocabulary ``kind`` is rejected at the inputSchema layer
  before the handler runs (jsonschema enum), surfacing as -32602.
* The annotate tool's description carries the bootstrap precondition
  and names ``meho.topology.create_node`` as the remediation path —
  the documentation half of the issue's "an agent reading only the
  description knows the entry point" criterion.
* End-to-end **empty-tenant bootstrap**: two ``create_node`` calls
  then one ``meho.topology.annotate`` call land a curated edge with
  no probe / refresh in the loop. Closes the consumer's
  ``-32602 no graph_node matched 'rdc-vault'`` dead-end.

The MCP-level happy paths exercise the SQLite-migrated test DB
end-to-end (same shape :mod:`tests.test_mcp_tools_topology_annotate`
uses for the annotate admin tools).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphEdge, GraphNode, Tenant
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# Patch site — broadcast publisher in the service module.
_PUBLISH_NODES_PATCH = "meho_backplane.topology.nodes.publish_event"
_PUBLISH_ANNOTATE_PATCH = "meho_backplane.topology.annotate.publish_event"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    """Insert the operator's :class:`Tenant` row so the create_node FK resolves."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            Tenant(
                id=OPERATOR_TENANT_ID,
                slug="op-tenant",
                name="Op Tenant",
            ),
        )
    yield


def _create_node_call(client: TestClient, call_id: int, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "meho.topology.create_node", "arguments": arguments},
        },
    )


def _annotate_call(client: TestClient, call_id: int, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "meho.topology.annotate", "arguments": arguments},
        },
    )


# ---------------------------------------------------------------------------
# Registration + RBAC
# ---------------------------------------------------------------------------


def test_create_node_tool_registers_with_tenant_admin_and_write() -> None:
    """The admin tool lands with TENANT_ADMIN gate + write op_class."""
    entry = get_tool("meho.topology.create_node")
    assert entry is not None, "meho.topology.create_node not registered"
    defn, _handler = entry
    assert defn.required_role == TenantRole.TENANT_ADMIN
    assert defn.op_class == "write"


def test_create_node_input_schema_kind_is_pattern_constrained() -> None:
    """The inputSchema's ``kind`` carries the slug pattern, not an enum.

    T1 #2534 drift guard: the open vocabulary means the schema
    constrains by shape (``pattern`` + length bounds mirroring
    :data:`KIND_SLUG_PATTERN`) and the well-known kinds appear only as
    description suggestions. A reintroduced ``enum`` would silently
    re-close the vocabulary at the MCP boundary.
    """
    from meho_backplane.db.models import (
        KIND_SLUG_MAX_LENGTH,
        KIND_SLUG_MIN_LENGTH,
        KIND_SLUG_PATTERN,
        WELL_KNOWN_NODE_KINDS,
    )

    entry = get_tool("meho.topology.create_node")
    assert entry is not None
    defn, _ = entry
    kind_schema = defn.inputSchema["properties"]["kind"]
    assert "enum" not in kind_schema
    assert kind_schema["pattern"] == KIND_SLUG_PATTERN
    assert kind_schema["minLength"] == KIND_SLUG_MIN_LENGTH
    assert kind_schema["maxLength"] == KIND_SLUG_MAX_LENGTH
    # The well-known kinds survive as description suggestions.
    for kind in WELL_KNOWN_NODE_KINDS:
        assert kind in kind_schema["description"]


def test_annotate_description_states_bootstrap_precondition() -> None:
    """The annotate tool description carries the precondition + remediation.

    The documentation half of the issue's acceptance criterion: "an
    agent reading only the description knows the entry point". Pin
    the load-bearing tokens so a future edit doesn't silently drop
    them.
    """
    entry = get_tool("meho.topology.annotate")
    assert entry is not None
    defn, _ = entry
    description = defn.description
    # The "REQUIRES" callout naming the missing-endpoint failure mode.
    assert "REQUIRES" in description
    assert "graph_node" in description
    assert "no graph_node matched" in description
    # The two remediation paths must be named so an agent picks one.
    assert "meho.topology.create_node" in description
    assert "meho topology refresh" in description


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_create_node_hidden_from_non_admin_tools_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator-role session does not see the admin tool in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho.topology.create_node" not in names
    # Sibling read-half tools stay visible.
    assert "query_topology" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_create_node_visible_to_tenant_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A tenant_admin session sees the admin tool in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho.topology.create_node" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_create_node_call_from_non_admin_is_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """tools/call meho.topology.create_node from an operator → -32602 forbidden."""
    client, _op = client_with_operator
    response = _create_node_call(client, 3, {"kind": "vault-role", "name": "rdc-vault"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# meho.topology.create_node — end-to-end via the SQLite test DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_create_node_inserts_row_and_returns_was_created_true(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """tools/call meho.topology.create_node {...} on empty tenant → fresh row."""
    client, _op = client_with_operator

    with patch(_PUBLISH_NODES_PATCH, new=AsyncMock()) as publish_mock:
        response = _create_node_call(
            client,
            10,
            {
                "kind": "vault-role",
                "name": "rdc-vault",
                "note": "INVENTORY.md L42",
                "evidence_url": "https://example.test/inv#L42",
            },
        )

    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "vault-role"
    assert payload["name"] == "rdc-vault"
    assert payload["source"] == "curated"
    assert payload["was_created"] is True
    assert uuid.UUID(payload["node_id"])  # valid UUID

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = (
            (
                await session.execute(
                    select(GraphNode).where(GraphNode.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].properties["note"] == "INVENTORY.md L42"

    # One service-level broadcast.
    assert publish_mock.await_count == 1


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_create_node_is_idempotent_on_repeat(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A second tools/call with the same (kind, name) → was_created=false."""
    client, _op = client_with_operator

    with patch(_PUBLISH_NODES_PATCH, new=AsyncMock()):
        first = _create_node_call(client, 20, {"kind": "vault-role", "name": "rdc-vault"})
        second = _create_node_call(
            client,
            21,
            {"kind": "vault-role", "name": "rdc-vault", "note": "second"},
        )

    first_payload = json.loads(first.json()["result"]["content"][0]["text"])
    second_payload = json.loads(second.json()["result"]["content"][0]["text"])
    assert first_payload["was_created"] is True
    assert second_payload["was_created"] is False
    assert first_payload["node_id"] == second_payload["node_id"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_create_node_rejects_malformed_kind_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A malformed `kind` slug fails the inputSchema pattern (-32602)."""
    client, _op = client_with_operator
    response = _create_node_call(client, 30, {"kind": "DNS Record!", "name": "entangled"})
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_create_node_accepts_novel_kind_keycloak_realm(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """`keycloak-realm` — the pre-T1 #2534 doc-vs-enum drift — now round-trips.

    The annotate tool's description advertised `keycloak-realm` as a
    seedable kind while the closed enum rejected it at the inputSchema
    layer. With the open vocabulary the advertised example must work
    end-to-end.
    """
    client, _op = client_with_operator
    response = _create_node_call(client, 32, {"kind": "keycloak-realm", "name": "master"})
    body = response.json()
    assert "error" not in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "keycloak-realm"
    assert payload["name"] == "master"
    assert payload["was_created"] is True


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_create_node_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``tenant_id`` smuggled in arguments → -32602 (additionalProperties=false)."""
    client, _op = client_with_operator
    response = _create_node_call(
        client,
        31,
        {
            "kind": "vault-role",
            "name": "rdc-vault",
            "tenant_id": str(uuid.uuid4()),
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_create_node_rejects_empty_name(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Empty ``name`` fails the inputSchema minLength=1 (-32602)."""
    client, _op = client_with_operator
    response = _create_node_call(client, 32, {"kind": "vault-role", "name": ""})
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Empty-tenant bootstrap → annotate end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_bootstrap_then_annotate_round_trip_via_mcp(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """Issue's bootstrap acceptance criterion at the MCP layer.

    Empty tenant → ``meho.topology.create_node`` twice → ``meho.topology.
    annotate`` once → curated edge lands without any probe / refresh in
    the loop. Closes the ``-32602 no graph_node matched 'rdc-vault'``
    dead-end the consumer hit in the 2026-05-21 dogfood.
    """
    client, _op = client_with_operator

    with (
        patch(_PUBLISH_NODES_PATCH, new=AsyncMock()),
        patch(_PUBLISH_ANNOTATE_PATCH, new=AsyncMock()),
    ):
        node_one = _create_node_call(client, 40, {"kind": "principal", "name": "k8s-sa-prod"})
        node_two = _create_node_call(client, 41, {"kind": "vault-role", "name": "rdc-vault"})
        edge = _annotate_call(
            client,
            42,
            {
                "from_name": "k8s-sa-prod",
                "from_node_kind": "principal",
                "kind": "authenticates-via",
                "to_name": "rdc-vault",
                "to_node_kind": "vault-role",
                "note": "bootstrap",
            },
        )

    assert node_one.json()["result"]["isError"] is False
    assert node_two.json()["result"]["isError"] is False
    assert edge.json()["result"]["isError"] is False
    edge_payload = json.loads(edge.json()["result"]["content"][0]["text"])
    assert edge_payload["from"]["name"] == "k8s-sa-prod"
    assert edge_payload["to"]["name"] == "rdc-vault"
    assert edge_payload["kind"] == "authenticates-via"
    assert edge_payload["source"] == "curated"

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        nodes = (
            (
                await session.execute(
                    select(GraphNode).where(GraphNode.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
        edges = (
            (
                await session.execute(
                    select(GraphEdge).where(GraphEdge.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
    # Two nodes, one curated edge — the bootstrap end state.
    assert len(nodes) == 2
    assert len(edges) == 1
    assert edges[0].source == "curated"


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_annotate_missing_endpoint_still_returns_node_not_found(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """Issue AC: the unchanged ``no graph_node matched`` error path for genuine misses.

    With create_node available, the empty-tenant bootstrap stops being
    a dead end — but the annotate verb still surfaces a clear -32602
    when an operator references a name that genuinely doesn't exist
    in the tenant (the typo / wrong-tenant case the existing error
    message is there for).
    """
    client, _op = client_with_operator

    with patch(_PUBLISH_ANNOTATE_PATCH, new=AsyncMock()):
        response = _annotate_call(
            client,
            50,
            {
                "from_name": "ghost-principal",
                "kind": "authenticates-via",
                "to_name": "ghost-vault",
            },
        )

    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "no graph_node matched" in body["error"]["message"]
