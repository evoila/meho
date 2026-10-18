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
  a ``platform_admin`` + ``tenant`` works cross-tenant (#1641); a
  non-platform-admin (any rank, incl. ``tenant_admin``) passing a
  foreign ``tenant`` is rejected — naming one's own tenant is allowed.
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
from datetime import UTC, datetime, timedelta
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
from meho_backplane.topology.query import BoundedClosure, ClosureBudget
from meho_backplane.topology.resolvers import NodeNotFoundError
from meho_backplane.topology.schemas import (
    TopologyEdge,
    TopologyEdgeEndpoint,
    TopologyNode,
    TopologyPath,
)
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# Both closure kinds run through one bounded traversal (#3764); the
# two names keep each test's intent readable.
_DEPENDENTS_PATCH = "meho_backplane.mcp.tools.topology.bounded_closure"
_DEPENDENCIES_PATCH = "meho_backplane.mcp.tools.topology.bounded_closure"
_PATH_PATCH = "meho_backplane.mcp.tools.topology.find_path"

_OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-0000000000b0")


def _closure(nodes: list[TopologyNode]) -> AsyncMock:
    """A ``bounded_closure`` mock that returns *nodes* as a complete closure."""
    root = nodes[0] if nodes else _node("root", "vm", 0, None)
    return AsyncMock(return_value=BoundedClosure(root, tuple(nodes), None, 16, ClosureBudget()))


def _node(name: str, kind: str, depth: int, via: str | None) -> TopologyNode:
    return TopologyNode(
        id=UUID(int=depth + 1),
        kind=kind,
        name=name,
        source="auto",
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
    # walk with full snapshot payload). The closure / path branches and
    # their conditional requireds stay unchanged.
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
    ``meho_topology_unannotate`` (oneOf), and catches any future tool that
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
    mock_dep = _closure(nodes)
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
    mock_deps = _closure([_node("vm-1", "vm", 0, None)])
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
        "reverse": False,
        "kind": "vm",
        "depth": 4,
        "kind_filter": "runs-on",
        "relation_class": None,
        "include_stale": True,
    }


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_closure_forwards_include_stale_false(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """#2538: `include_stale: false` reaches the closure verb as a bool."""
    client, _op = client_with_operator
    mock_dep = _closure([_node("vm-1", "vm", 0, None)])
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 16,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "dependents",
                        "target": "vm-1",
                        "include_stale": False,
                    },
                },
            },
        )
    assert response.json()["result"]["isError"] is False
    assert mock_dep.await_args.kwargs["include_stale"] is False


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_forwards_include_stale_false(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """#2538: `include_stale: false` reaches ``find_path`` as a kwarg."""
    client, _op = client_with_operator
    mock_path = AsyncMock(return_value=None)
    with patch(_PATH_PATCH, new=mock_path):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 17,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "path",
                        "from_name": "a",
                        "to_name": "b",
                        "include_stale": False,
                    },
                },
            },
        )
    assert response.json()["result"]["isError"] is False
    assert mock_path.await_args.kwargs["include_stale"] is False


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_dependents_untracked_returns_typed_envelope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """G0.18-T4 (#1357, RDC #789 N2). When the substrate raises
    :class:`NodeNotFoundError` for an untracked anchor the closure
    verb returns a successful tool call with a typed
    ``{status: "node_untracked"}`` envelope rather than -32602.

    Pre-fix behaviour treated the miss as an operator input error and
    raised -32602; the agent retried with corrections instead of
    rendering "the anchor isn't in the topology graph yet — refresh
    or annotate first." The new envelope is what lets an agent stop
    misreading an empty closure as "safe to delete" for the
    blast-radius use case.
    """
    client, _op = client_with_operator
    mock_dep = AsyncMock(side_effect=NodeNotFoundError("vault-prod"))
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "dependents",
                        "target": "vault-prod",
                    },
                },
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "dependents"
    assert payload["status"] == "node_untracked"
    assert payload["name"] == "vault-prod"
    assert payload["nodes"] == []
    assert "node_kind" not in payload  # no kind pin was supplied


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_dependencies_untracked_echoes_node_kind_when_supplied(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The ``node_untracked`` envelope echoes ``node_kind`` when the
    caller supplied one — same self-contained-diagnostic discipline
    the REST 404 follows.
    """
    client, _op = client_with_operator
    mock_deps = AsyncMock(side_effect=NodeNotFoundError("vc-prod", kind="target"))
    with patch(_DEPENDENCIES_PATCH, new=mock_deps):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {
                    "name": "query_topology",
                    "arguments": {
                        "kind": "dependencies",
                        "target": "vc-prod",
                        "node_kind": "target",
                    },
                },
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "dependencies"
    assert payload["status"] == "node_untracked"
    assert payload["name"] == "vc-prod"
    assert payload["node_kind"] == "target"
    assert payload["nodes"] == []


@pytest.mark.parametrize(
    ("kind", "patch_target", "direction"),
    [
        ("dependents", _DEPENDENTS_PATCH, "reverse"),
        ("dependencies", _DEPENDENCIES_PATCH, "forward"),
    ],
)
@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_closure_untracked_carries_direction_filters_and_evidence(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    kind: str,
    patch_target: str,
    direction: str,
) -> None:
    """#3760: an untracked closure root still reports the direction, the
    relation filter and the stale view the lookup used, like every other
    closure answer."""
    client, _op = client_with_operator
    mock_verb = AsyncMock(side_effect=NodeNotFoundError("ghost"))
    with patch(patch_target, new=mock_verb):
        payload = _payload(
            _call_query_topology(
                client,
                {
                    "kind": kind,
                    "target": "ghost",
                    "include_stale": False,
                    "relation_class": "placement",
                },
            )
        )
    assert mock_verb.await_args.kwargs["include_stale"] is False
    assert mock_verb.await_args.kwargs["relation_class"] == "placement"
    assert payload["kind"] == kind
    assert payload["status"] == "node_untracked"
    assert payload["name"] == "ghost"
    assert payload["nodes"] == []
    assert payload["direction"] == direction
    assert payload["relation_class"] == "placement"
    evidence = payload["evidence"]
    assert evidence["stale_rows"] == "excluded"
    assert "include_stale is false" in evidence["stale_rows_note"]
    assert "the root you named" in evidence["stale_rows_note"]
    assert "not the vendor's own timestamp" in evidence["observation_note"]
    assert evidence["freshness_meaning"] == {}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_dependents_tracked_no_deps_omits_status(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A tracked-but-no-dependents anchor keeps the historical
    ``{kind, nodes: [root]}`` shape — no ``status`` field, no
    envelope drift. Pin G0.18-T4 (#1357)'s structural distinction
    between untracked (status set) and tracked-empty (status omitted).
    """
    client, _op = client_with_operator
    mock_dep = _closure([_node("ns-prod-foo", "namespace", 0, None)])
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
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
                        "target": "ns-prod-foo",
                    },
                },
            },
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "dependents"
    assert "status" not in payload
    assert [n["name"] for n in payload["nodes"]] == ["ns-prod-foo"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_reports_found_or_no_route(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=path returns the TopologyPath; an unreachable pair says ``no_route``."""
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
    assert payload["status"] == "found"
    assert payload["direction"] == "undirected"
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
    assert miss_payload["kind"] == "path"
    assert miss_payload["status"] == "no_route"
    assert miss_payload["path"] is None
    assert miss_payload["direction"] == "undirected"
    assert "no undirected route within 8 hops" in miss_payload["detail"]
    assert "stale rows included, relation class any" in miss_payload["detail"]
    assert miss_payload["evidence"]["stale_rows"] == "included"


def _call_query_topology(client: TestClient, arguments: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"name": "query_topology", "arguments": arguments},
        },
    )
    return response.json()


def _payload(body: dict[str, Any]) -> dict[str, Any]:
    assert body["result"]["isError"] is False
    return json.loads(body["result"]["content"][0]["text"])


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_missing_endpoint_is_not_no_route(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """#3760: a missing endpoint and an unreachable one get different statuses."""
    client, _op = client_with_operator
    mock_path = AsyncMock(side_effect=NodeNotFoundError("ghost", kind="vm"))
    with patch(_PATH_PATCH, new=mock_path):
        payload = _payload(
            _call_query_topology(
                client,
                {"kind": "path", "from_name": "a", "to_name": "ghost", "to_node_kind": "vm"},
            )
        )
    assert mock_path.await_args.kwargs["missing_endpoint_raises"] is True
    assert payload["status"] == "node_untracked"
    assert payload["name"] == "ghost"
    assert payload["node_kind"] == "vm"
    assert payload["path"] is None
    assert "not in the topology graph" in payload["detail"]
    # Every path outcome says which stale view the search used.
    assert payload["evidence"]["stale_rows"] == "included"
    assert "include_stale is true" in payload["evidence"]["stale_rows_note"]
    assert payload["evidence"]["freshness_meaning"] == {}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_no_route_and_untracked_carry_selected_stale_view(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A ``no_route`` under ``include_stale=false`` may be caused by an
    excluded stale edge, so the answer must say that stale rows were
    excluded. ``node_untracked`` carries the same context."""
    client, _op = client_with_operator
    base = {
        "kind": "path",
        "from_name": "a",
        "to_name": "b",
        "include_stale": False,
        "relation_class": "placement",
        "path_direction": "forward",
    }
    with patch(_PATH_PATCH, new=AsyncMock(return_value=None)):
        no_route = _payload(_call_query_topology(client, base))
    assert no_route["status"] == "no_route"
    assert "no forward route within 8 hops" in no_route["detail"]
    assert "stale rows excluded, relation class placement" in no_route["detail"]
    assert "under other filters" in no_route["detail"]
    evidence = no_route["evidence"]
    assert evidence["stale_rows"] == "excluded"
    assert "include_stale is false" in evidence["stale_rows_note"]
    assert "the two endpoints you named" in evidence["stale_rows_note"]
    assert "not the vendor's own timestamp" in evidence["observation_note"]
    assert evidence["freshness_meaning"] == {}

    with patch(_PATH_PATCH, new=AsyncMock(side_effect=NodeNotFoundError("b"))):
        untracked = _payload(_call_query_topology(client, base))
    assert untracked["status"] == "node_untracked"
    assert untracked["evidence"]["stale_rows"] == "excluded"
    assert "the two endpoints you named" in untracked["evidence"]["stale_rows_note"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_path_stale_endpoint_is_reported_with_filter_off(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``find_path`` returns the two named endpoints even when they are
    stale and ``include_stale=false`` (#2538). The evidence must not say
    the stale row is there because ``include_stale`` is true."""
    client, _op = client_with_operator
    now = datetime.now(UTC)
    found = TopologyPath(
        nodes=(
            TopologyNode(
                id=UUID(int=1),
                kind="vm",
                name="a",
                source="auto",
                depth=0,
                via_edge_kind=None,
                last_seen=None,
            ),
            TopologyNode(
                id=UUID(int=2),
                kind="host",
                name="b",
                source="auto",
                depth=1,
                via_edge_kind="runs-on",
                parent_node_id=UUID(int=1),
                via_edge_id=UUID(int=11),
                via_edge_source="auto",
                via_edge_last_seen=now,
                last_seen=now,
            ),
        ),
        total_hops=1,
    )
    mock_path = AsyncMock(return_value=found)
    with patch(_PATH_PATCH, new=mock_path):
        payload = _payload(
            _call_query_topology(
                client,
                {"kind": "path", "from_name": "a", "to_name": "b", "include_stale": False},
            )
        )
    assert mock_path.await_args.kwargs["include_stale"] is False
    assert payload["status"] == "found"
    start, end = payload["path"]["nodes"]
    assert start["freshness"] == "stale"
    assert end["freshness"] == "observed"
    evidence = payload["evidence"]
    assert evidence["stale_rows"] == "excluded"
    note = evidence["stale_rows_note"]
    assert "the two endpoints you named, which MEHO always shows, even when stale" in note
    stale_meaning = evidence["freshness_meaning"]["stale"]
    assert "include_stale is true" not in stale_meaning
    assert "shown only because the call named it" in stale_meaning


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("direction", ["forward", "reverse", "undirected"])
def test_query_topology_path_forwards_and_echoes_direction(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    direction: str,
) -> None:
    """#3760: ``path_direction`` and ``relation_class`` reach ``find_path``."""
    client, _op = client_with_operator
    found = TopologyPath(
        nodes=(_node("a", "vm", 0, None), _node("b", "host", 1, "runs-on")),
        total_hops=1,
        direction=direction,  # type: ignore[arg-type]
    )
    mock_path = AsyncMock(return_value=found)
    with patch(_PATH_PATCH, new=mock_path):
        payload = _payload(
            _call_query_topology(
                client,
                {
                    "kind": "path",
                    "from_name": "a",
                    "to_name": "b",
                    "path_direction": direction,
                    "relation_class": "placement",
                },
            )
        )
    assert mock_path.await_args.kwargs["direction"] == direction
    assert mock_path.await_args.kwargs["relation_class"] == "placement"
    assert payload["direction"] == direction
    assert payload["path"]["direction"] == direction
    assert payload["relation_class"] == "placement"
    assert payload["path"]["nodes"][1]["via_relation_class"] == "placement"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize(
    "bad",
    [{"path_direction": "sideways"}, {"relation_class": "service"}],
)
def test_query_topology_rejects_unknown_direction_or_class(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    bad: dict[str, str],
) -> None:
    client, _op = client_with_operator
    mock_path = AsyncMock(return_value=None)
    with patch(_PATH_PATCH, new=mock_path):
        body = _call_query_topology(
            client, {"kind": "path", "from_name": "a", "to_name": "b", **bad}
        )
    assert body["error"]["code"] == INVALID_PARAMS
    mock_path.assert_not_awaited()


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_closure_rows_carry_class_source_and_freshness(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """#3760: stale, old, observed and curated rows read differently."""
    client, _op = client_with_operator
    now = datetime.now(UTC)
    scope = "target:0f8fad5b-d9cb-469f-a165-70867728950e"
    nodes = [
        TopologyNode(
            id=UUID(int=1),
            kind="namespace",
            name="ns",
            source="auto",
            depth=0,
            via_edge_kind=None,
            source_scope=scope,
            last_seen=now,
        ),
        # Pod seen recently, over a containment edge that the latest
        # refresh no longer reported.
        TopologyNode(
            id=UUID(int=2),
            kind="pod",
            name="pod-1",
            source="auto",
            depth=1,
            via_edge_kind="belongs-to",
            parent_node_id=UUID(int=1),
            via_edge_id=UUID(int=11),
            via_edge_source="auto",
            via_edge_last_seen=None,
            source_scope=scope,
            last_seen=now - timedelta(minutes=5),
        ),
        # Service last seen long ago, over a curated edge of a kind
        # MEHO does not map.
        TopologyNode(
            id=UUID(int=3),
            kind="service",
            name="svc-1",
            source="auto",
            depth=1,
            via_edge_kind="depends-on",
            parent_node_id=UUID(int=1),
            via_edge_id=UUID(int=12),
            via_edge_source="curated",
            via_edge_last_seen=now - timedelta(days=40),
            source_scope=scope,
            last_seen=now - timedelta(days=3),
        ),
    ]
    mock_dep = _closure(nodes)
    with patch(_DEPENDENTS_PATCH, new=mock_dep):
        payload = _payload(
            _call_query_topology(
                client,
                {"kind": "dependents", "target": "ns", "relation_class": "containment"},
            )
        )
    assert mock_dep.await_args.kwargs["relation_class"] == "containment"
    assert payload["direction"] == "reverse"
    assert payload["relation_class"] == "containment"
    root, pod, svc = payload["nodes"]
    assert root["source_scope"] == scope
    assert root["freshness"] == "observed"
    assert root["via_edge_freshness"] is None
    assert root["via_relation_class"] is None
    assert pod["via_relation_class"] == "containment"
    assert pod["freshness"] == "observed"
    assert pod["via_edge_freshness"] == "stale"
    assert svc["via_relation_class"] == "unknown"
    assert svc["freshness"] == "old_observation"
    assert svc["via_edge_freshness"] == "asserted"
    assert svc["via_edge_source"] == "curated"

    evidence = payload["evidence"]
    assert evidence["stale_rows"] == "included"
    assert "not the vendor's own timestamp" in evidence["observation_note"]
    assert set(evidence["freshness_meaning"]) == {
        "observed",
        "stale",
        "old_observation",
        "asserted",
    }


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("kind", ["dependents", "dependencies"])
def test_query_topology_stale_root_is_reported_with_filter_off(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    kind: str,
) -> None:
    """The closure root is never filtered by ``include_stale`` (#2538), so
    a stale root appears with ``include_stale=false``. The evidence must
    name that exception instead of claiming the row came from
    ``include_stale=true``."""
    client, _op = client_with_operator
    root = TopologyNode(
        id=UUID(int=1),
        kind="namespace",
        name="ns",
        source="auto",
        depth=0,
        via_edge_kind=None,
        last_seen=None,
    )
    mock_verb = _closure([root])
    target = _DEPENDENTS_PATCH if kind == "dependents" else _DEPENDENCIES_PATCH
    with patch(target, new=mock_verb):
        payload = _payload(
            _call_query_topology(client, {"kind": kind, "target": "ns", "include_stale": False})
        )
    assert mock_verb.await_args.kwargs["include_stale"] is False
    (row,) = payload["nodes"]
    assert row["freshness"] == "stale"
    evidence = payload["evidence"]
    assert evidence["stale_rows"] == "excluded"
    note = evidence["stale_rows_note"]
    assert "include_stale is false" in note
    assert "the root you named, which MEHO always shows, even when stale" in note
    assert "include_stale is true" not in evidence["freshness_meaning"]["stale"]
    assert "shown only because the call named it" in evidence["freshness_meaning"]["stale"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_stale_rows_included_says_why_they_are_shown(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    root = TopologyNode(
        id=UUID(int=1),
        kind="namespace",
        name="ns",
        source="auto",
        depth=0,
        via_edge_kind=None,
        last_seen=None,
    )
    with patch(_DEPENDENTS_PATCH, new=_closure([root])):
        payload = _payload(_call_query_topology(client, {"kind": "dependents", "target": "ns"}))
    evidence = payload["evidence"]
    assert evidence["stale_rows"] == "included"
    assert "include_stale is true" in evidence["stale_rows_note"]
    stale_meaning = evidence["freshness_meaning"]["stale"]
    assert stale_meaning.endswith("It is shown because include_stale is true.")


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_edges_carry_relation_class_and_freshness(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    edge = TopologyEdge(
        id=UUID(int=5),
        from_endpoint=TopologyEdgeEndpoint(id=UUID(int=1), kind="sa", name="sa-1"),
        to_endpoint=TopologyEdgeEndpoint(id=UUID(int=2), kind="role", name="role-1"),
        kind="authenticates-via",
        source="curated",
        last_seen=datetime.now(UTC),
    )
    mock_edges = AsyncMock(return_value=[edge])
    with patch("meho_backplane.mcp.tools.topology.list_edges", new=mock_edges):
        payload = _payload(
            _call_query_topology(client, {"kind": "edges", "relation_class": "dependency"})
        )
    assert mock_edges.await_args.kwargs["relation_class"] == "dependency"
    assert payload["relation_class"] == "dependency"
    (row,) = payload["edges"]
    assert row["relation_class"] == "dependency"
    assert row["freshness"] == "asserted"
    assert payload["evidence"]["stale_rows"] == "excluded"
    assert payload["evidence"]["stale_rows_note"] == (
        "Rows the latest refresh no longer reported are never listed."
    )
    assert set(payload["evidence"]["freshness_meaning"]) == {"asserted"}


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
    mock_dep = _closure([])
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
                secret_ref="targets/topology-x",
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
    """A non-platform-admin operator naming a foreign tenant is rejected (-32602)."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_tenant(_OTHER_TENANT_ID, "other-tenant")
    await _seed_target(_OTHER_TENANT_ID, "other-vc", "vmware", "ovc.example")

    response = _list_targets_call(client, 22, {"tenant": "other-tenant"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "platform_admin" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_list_targets_tenant_admin_cannot_cross_tenant(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A `tenant_admin` (not platform-admin) naming a foreign tenant is denied (#1641).

    Cross-tenant enumeration moved off `tenant_admin` rank onto the
    `platform_admin` capability — a tenant-admin of A must not be able
    to enumerate B's targets (the cross-tenant IDOR this task closes).
    """
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_tenant(_OTHER_TENANT_ID, "other-tenant")
    await _seed_target(_OTHER_TENANT_ID, "other-vc", "vmware", "ovc.example")

    response = _list_targets_call(client, 23, {"tenant": "other-tenant"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "platform_admin" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [(TenantRole.TENANT_ADMIN, True)], indirect=True)
async def test_list_targets_platform_admin_cross_tenant_by_slug(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A `platform_admin` + `tenant` slug lists the other tenant's targets (#1641)."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_tenant(_OTHER_TENANT_ID, "other-tenant")
    await _seed_target(_OTHER_TENANT_ID, "other-vc", "vmware", "ovc.example")

    response = _list_targets_call(client, 23, {"tenant": "other-tenant"})
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["other-vc"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_own_tenant_slug_is_allowed(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Naming one's OWN tenant explicitly (slug) is allowed for any role (#1641)."""
    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_target(OPERATOR_TENANT_ID, "rdc-vcenter", "vmware", "vc.example")

    response = _list_targets_call(client, 25, {"tenant": "op-tenant"})
    body = response.json()
    assert "error" not in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["rdc-vcenter"]


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


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_excludes_soft_deleted(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Soft-deleted targets (G0.14-T4 #1145) are excluded from the MCP listing.

    Mirrors the REST list endpoint's ``deleted_at IS NULL`` filter so
    MCP and REST never disagree about which targets are visible to a
    tenant — the consumer's session-pinned target cache should not
    surface tombstoned rows.
    """
    from datetime import UTC, datetime

    client, _op = client_with_operator
    await _seed_tenant(OPERATOR_TENANT_ID, "op-tenant")
    await _seed_target(OPERATOR_TENANT_ID, "live", "vmware", "live.example")

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            TargetORM(
                tenant_id=OPERATOR_TENANT_ID,
                name="retired",
                aliases=[],
                product="vmware",
                host="retired.example",
                port=443,
                secret_ref="targets/topology-x",
                auth_model="shared_service_account",
                deleted_at=datetime.now(UTC),
            )
        )

    response = _list_targets_call(client, 27, {})
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert [t["name"] for t in payload["targets"]] == ["live"]
