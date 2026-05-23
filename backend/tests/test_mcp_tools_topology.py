# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G9.1-T7 (#455) topology MCP meta-tools.

Two tools, exactly two — ``query_topology`` (parametric) and
``list_targets``. Coverage maps to the issue acceptance criteria:

* Both meta-tools registered against G0.5's MCP server (visible in
  ``tools/list``); the per-shape ``topology.*`` tools are NOT
  registered (narrow-waist postulate).
* ``query_topology`` inputSchema is conditional on ``kind``:
  ``dependents`` / ``dependencies`` require ``target``; ``path``
  requires ``from_name`` + ``to_name`` — enforced at the dispatcher's
  ``jsonschema.Draft202012Validator`` layer.
* ``tools/call query_topology {kind: dependents, target: <node>}``
  dispatches through the T4 service and returns the dependents list.
* ``list_targets`` returns the operator's tenant's targets;
  ``tenant_admin`` + ``tenant`` works cross-tenant; an ``operator``
  passing ``tenant`` is rejected.
* Tool descriptions name the blast-radius use-case verbatim ("call
  this *before* recommending a destructive op").
* Tenant scope comes from the operator JWT, never the arguments dict.

The T4 service functions are patched at the tool's import site so the
route tests don't depend on a PG-seeded graph; the service itself is
covered by ``tests/test_topology_query_schemas.py`` +
``tests/integration/test_topology_query.py``. ``list_targets`` runs a
real ``select(TargetORM)`` against the SQLite-migrated test DB with
seeded rows (no substrate to patch — it is a direct query).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.mcp.registry import all_tools_for, get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.topology.schemas import TopologyNode, TopologyPath
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_DEPENDENTS_PATCH = "meho_backplane.mcp.tools.topology.find_dependents"
_DEPENDENCIES_PATCH = "meho_backplane.mcp.tools.topology.find_dependencies"
_PATH_PATCH = "meho_backplane.mcp.tools.topology.find_path"

_OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000b0")


def _node(name: str, kind: str, depth: int, via: str | None) -> TopologyNode:
    return TopologyNode(
        id=UUID(int=depth + 1),
        kind=kind,
        name=name,
        properties={},
        depth=depth,
        via_edge_kind=via,
    )


# ---------------------------------------------------------------------------
# Registration + narrow-waist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_tools_list_exposes_exactly_the_two_meta_tools(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Both meta-tools register; the per-shape variants do not."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "query_topology" in names
    assert "list_targets" in names
    # Narrow-waist postulate (CLAUDE.md #5): no per-shape topology tools.
    for forbidden in (
        "topology.dependents",
        "topology.dependencies",
        "topology.path",
        "topology.refresh",
        "targets.discover",
    ):
        assert forbidden not in names
        assert get_tool(forbidden) is None


def test_registered_definitions_are_operator_read_class() -> None:
    """Both tools declare operator role + read op_class (audit/broadcast contract)."""
    for tool_name in ("query_topology", "list_targets"):
        entry = get_tool(tool_name)
        assert entry is not None
        defn, _handler = entry
        assert defn.required_role == TenantRole.OPERATOR
        assert defn.op_class == "read"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_description_names_blast_radius_verbatim(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The load-bearing description names the blast-radius use-case verbatim."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    desc = tools["query_topology"]["description"]
    # Verbatim phrase from CLAUDE.md / the initiative body.
    assert "before* recommending a destructive op" in desc
    assert "kind=dependents" in desc
    list_desc = tools["list_targets"]["description"].lower()
    assert "before picking a target" in list_desc


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_input_schema_is_conditional_on_kind(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Per-kind required fields are enforced via allOf/if/then on the STORED schema.

    The conditional ``allOf`` lives on the *stored* ``inputSchema`` (what
    the dispatcher jsonschema-validates ``tools/call.arguments`` against),
    NOT on the wire shape: the Anthropic Messages API rejects a top-level
    ``allOf`` / ``oneOf`` / ``anyOf`` in a tool's ``input_schema`` and the
    rejection 400s the whole session (#905), so
    :meth:`ToolDefinition.to_wire` strips it from the published copy. This
    test pins both halves of that split — the wire shape carries no
    combinator, the stored schema still does (its live effect is proven by
    the two ``-32602`` tests below).
    """
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
    )
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    wire_schema = tools["query_topology"]["inputSchema"]
    assert wire_schema["required"] == ["kind"]
    assert wire_schema["additionalProperties"] is False
    # G9.2-T7 (#598) widened the enum with the `edges` facet (replaces a
    # standalone list_edges meta-tool); G9.3-T5 (#861) added the
    # `timeline` facet (tenant-wide chronological feed of graph
    # changes from the *_history tables); G9.3-T4 (#860) added the
    # `diff` facet (net per-resource delta between two timestamps);
    # G9.3-T3 (#859) added the `history` facet (per-resource history
    # walk with full snapshot payload). The closure / path branches
    # and their conditional requireds stay unchanged.
    assert wire_schema["properties"]["kind"]["enum"] == [
        "dependents",
        "dependencies",
        "path",
        "edges",
        "timeline",
        "diff",
        "history",
    ]
    # The wire shape carries NO top-level combinator — Anthropic 400s on
    # it (#905); the conditional logic moved to the stored schema below.
    assert "allOf" not in wire_schema
    assert "oneOf" not in wire_schema
    assert "anyOf" not in wire_schema
    # MEHO-internal fields stripped from the wire shape.
    assert "required_role" not in tools["query_topology"]
    assert "op_class" not in tools["query_topology"]

    # The conditional requireds survive on the stored schema (the one the
    # dispatcher validates against).
    entry = get_tool("query_topology")
    assert entry is not None
    stored_schema = entry[0].inputSchema
    conditionals = stored_schema["allOf"]
    # Skip per-kind ``limit.maximum`` tightening clauses (no ``required``
    # key) — those intersect a stricter ``limit`` ceiling for ``edges``
    # / ``timeline`` and aren't part of the required-field contract.
    by_kind = {
        c["if"]["properties"]["kind"]["const"]: c["then"]["required"]
        for c in conditionals
        if "required" in c["then"]
    }
    assert by_kind["dependents"] == ["target"]
    assert by_kind["dependencies"] == ["target"]
    assert sorted(by_kind["path"]) == ["from_name", "to_name"]
    # `diff` requires both timestamps.
    assert sorted(by_kind["diff"]) == ["ts1", "ts2"]
    # `history` requires `target` (the anchor node name); `edges` and
    # `timeline` have no required field — every filter is optional on
    # both facets.
    assert by_kind["history"] == ["target"]
    assert "edges" not in by_kind
    assert "timeline" not in by_kind


def test_no_published_tool_wire_schema_has_top_level_combinator() -> None:
    """Registry-wide invariant: no tool's wire ``inputSchema`` has a top-level combinator.

    The guard for #905 at the level it actually bites: every tool the
    backplane publishes via ``tools/list`` is forwarded verbatim by MCP
    clients (Claude Code) as a custom tool's ``input_schema``, and the
    Anthropic Messages API 400s the *entire* request if any one of them
    carries ``oneOf`` / ``allOf`` / ``anyOf`` at the top level. This walks
    the full registered tool set (tenant_admin sees every tool) and
    asserts each :meth:`ToolDefinition.to_wire` output is API-legal. It
    would have caught both ``query_topology`` (allOf) and
    ``meho.topology.unannotate`` (oneOf), and catches any future tool that
    reintroduces the pattern at the source rather than at first 400.
    """
    forbidden = ("oneOf", "allOf", "anyOf")
    offenders = {
        defn.name: [k for k in forbidden if k in defn.to_wire()["inputSchema"]]
        for defn in all_tools_for(build_operator(TenantRole.TENANT_ADMIN))
        if any(k in defn.to_wire()["inputSchema"] for k in forbidden)
    }
    assert offenders == {}, (
        "tools publish a top-level JSON-Schema combinator in their wire "
        f"inputSchema (Anthropic Messages API rejects these): {offenders}"
    )


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_without_endpoints_rejected_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=path with no from_name/to_name → -32602 before the handler runs."""
    client, _op = client_with_operator
    mock_path = AsyncMock()
    with patch(_PATH_PATCH, new=mock_path):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "query_topology", "arguments": {"kind": "path"}},
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    mock_path.assert_not_awaited()


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_dependents_without_target_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=dependents with no target → -32602 at the schema layer."""
    client, _op = client_with_operator
    mock_dep = AsyncMock()
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {"kind": "dependents"},
                },
            },
        )
    assert response.json()["error"]["code"] == INVALID_PARAMS
    mock_dep.assert_not_awaited()


# ---------------------------------------------------------------------------
# query_topology dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_dependents_returns_node_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=dependents dispatches find_dependents and returns its closure."""
    client, op = client_with_operator
    nodes = [
        _node("customer-a-prod-foo", "namespace", 0, None),
        _node("svc-a", "service", 1, "belongs-to"),
    ]
    mock_dep = AsyncMock(return_value=nodes)
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "dependents",
                        "target": "customer-a-prod-foo",
                    },
                },
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "dependents"
    assert [n["name"] for n in payload["nodes"]] == [
        "customer-a-prod-foo",
        "svc-a",
    ]
    mock_dep.assert_awaited_once()
    # Tenant scope comes from the operator, not the arguments dict.
    assert mock_dep.await_args.args[0] is op
    assert mock_dep.await_args.args[1] == "customer-a-prod-foo"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_dependencies_passes_filters_through(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=dependencies forwards depth / kind_filter / node_kind to T4."""
    client, _op = client_with_operator
    mock_deps = AsyncMock(return_value=[_node("vm-1", "vm", 0, None)])
    with patch(_DEPENDENCIES_PATCH, new=mock_deps):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "dependencies",
                        "target": "vm-1",
                        "depth": 4,
                        "kind_filter": "runs-on",
                        "node_kind": "vm",
                    },
                },
            },
        )
    assert response.json()["result"]["isError"] is False
    assert mock_deps.await_args.kwargs == {
        "kind": "vm",
        "depth": 4,
        "kind_filter": "runs-on",
    }


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_returns_path_or_null(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=path returns the TopologyPath; an unreachable pair returns null."""
    client, _op = client_with_operator
    found = TopologyPath(
        nodes=(
            _node("a", "service", 0, None),
            _node("b", "datastore", 1, "mounts"),
        ),
        total_hops=1,
    )
    with patch(_PATH_PATCH, new=AsyncMock(return_value=found)):
        ok = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "path",
                        "from_name": "a",
                        "to_name": "b",
                    },
                },
            },
        )
    payload = json.loads(ok.json()["result"]["content"][0]["text"])
    assert payload["kind"] == "path"
    assert [n["name"] for n in payload["path"]["nodes"]] == ["a", "b"]

    with patch(_PATH_PATCH, new=AsyncMock(return_value=None)):
        miss = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "path",
                        "from_name": "a",
                        "to_name": "z",
                    },
                },
            },
        )
    miss_payload = json.loads(miss.json()["result"]["content"][0]["text"])
    assert miss_payload == {"kind": "path", "path": None}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_ambiguous_node_maps_to_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AmbiguousNodeError → -32602 naming the candidate kinds."""
    from meho_backplane.topology.query import AmbiguousNodeError

    client, _op = client_with_operator
    mock_dep = AsyncMock(side_effect=AmbiguousNodeError("app", ["target", "vm"]))
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {"kind": "dependents", "target": "app"},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "ambiguous" in body["error"]["message"].lower()


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """additionalProperties: false blocks a smuggled tenant_id."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "query_topology",
                "arguments": {
                    "kind": "dependents",
                    "target": "x",
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                },
            },
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.READ_ONLY], indirect=True)
def test_query_topology_read_only_role_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """read_only is below the operator gate."""
    client, _op = client_with_operator
    mock_dep = AsyncMock(return_value=[])
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 16,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {"kind": "dependents", "target": "x"},
                },
            },
        )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    mock_dep.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_targets — runs a real query against the migrated test DB
# ---------------------------------------------------------------------------


async def _seed_tenant(tenant_id: UUID, slug: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=slug, name=slug))


async def _seed_target(
    tenant_id: UUID,
    name: str,
    product: str,
    host: str,
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            TargetORM(
                tenant_id=tenant_id,
                name=name,
                aliases=[],
                product=product,
                host=host,
                port=443,
                secret_ref="vault:kv/data/x",
                auth_model="shared_service_account",
            )
        )


def _list_targets_call(
    client: TestClient,
    call_id: int,
    arguments: dict[str, Any],
) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "list_targets", "arguments": arguments},
        },
    )


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_returns_own_tenant_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """list_targets returns the operator's tenant's targets, name-ordered."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_target(OPERATOR_TENANT_ID, "rdc-vcenter", "vmware", "vc.example")
    await _seed_target(OPERATOR_TENANT_ID, "rdc-vault", "vault", "vault.example")

    response = _list_targets_call(client, 20, {})
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["rdc-vault", "rdc-vcenter"]
    assert payload["next_cursor"] is None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_connector_id_filters_by_product(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """connector_id is canonicalised to its product and filters the rows."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_target(OPERATOR_TENANT_ID, "rdc-vcenter", "vmware", "vc.example")
    await _seed_target(OPERATOR_TENANT_ID, "rdc-vault", "vault", "vault.example")

    response = _list_targets_call(client, 21, {"connector_id": "vmware-rest-9.0"})
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["rdc-vcenter"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_operator_cannot_cross_tenant(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An operator passing `tenant` is rejected (-32602), not silently scoped."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")

    response = _list_targets_call(client, 22, {"tenant": "other-tenant"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "tenant_admin" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_list_targets_tenant_admin_cross_tenant_by_slug(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """tenant_admin + `tenant` slug lists the other tenant's targets."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_tenant(_OTHER_TENANT_ID, "other-tenant")
    await _seed_target(_OTHER_TENANT_ID, "other-vc", "vmware", "ovc.example")

    response = _list_targets_call(client, 23, {"tenant": "other-tenant"})
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["other-vc"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_list_targets_unknown_tenant_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An unknown `tenant` surfaces as -32602, not an empty list."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")

    response = _list_targets_call(client, 24, {"tenant": "no-such-tenant"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "no tenant matches" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_keyset_pagination(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A full page surfaces a next_cursor; the follow-up page resumes after it."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    for n in ("t-a", "t-b", "t-c"):
        await _seed_target(OPERATOR_TENANT_ID, n, "vmware", f"{n}.example")

    page1 = json.loads(
        _list_targets_call(client, 25, {"limit": 2}).json()["result"]["content"][0]["text"]
    )
    assert [t["name"] for t in page1["targets"]] == ["t-a", "t-b"]
    assert page1["next_cursor"] == "t-b"

    page2 = json.loads(
        _list_targets_call(client, 26, {"limit": 2, "cursor": "t-b"}).json()["result"]["content"][
            0
        ]["text"]
    )
    assert [t["name"] for t in page2["targets"]] == ["t-c"]
    assert page2["next_cursor"] is None
