# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-tool payload conformance to declared MCP ``outputSchema`` (#2774).

MCP 2025-06-18 §Tools/Output Schema: a declaring tool **MUST** return
structured results conforming to its schema; clients SHOULD validate.
Claude's frontend enforces this — a non-conforming (or absent)
``structuredContent`` hard-fails the call client-side while the server
logs 200. Before this suite the declared schemas had never been
exercised against real payloads (the ``query_audit`` ``shape="tree"``
drift was found exactly this way), so every scenario here drives a
**real handler result** through the full ``tools/call`` dispatcher and
validates the emitted ``structuredContent`` against the declared
schema with the same Draft 2020-12 validator + format checker the
input side uses.

Companion files ("machine-check the Anthropic contract" family, after
#2644 / #2745):

* ``tests/test_mcp_structured_content.py`` — dispatcher emission rules,
  schema well-formedness, and the pinned declaring-tool roster.
* ``tests/test_published_tool_schema_shape.py`` — input-schema shape.

Coverage is total by construction:
:func:`test_every_declaring_tool_has_a_conformance_scenario` fails when
a registry tool declares an ``outputSchema`` without a scenario here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import jsonschema
import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools.result_query as result_query_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.result_handle_store import SpilledWindow
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import GraphNode, Tenant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.main import app
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.topology.schemas import TopologyEdge, TopologyEdgeEndpoint
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
    seed_doc_collection,
    seeded_operator_tenant,  # noqa: F401 -- pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"

#: Patch sites for the fail-open broadcast publishers inside the
#: topology write services (the writes themselves are real).
_PUBLISH_ANNOTATE = "meho_backplane.topology.annotate.publish_event"
_PUBLISH_NODES = "meho_backplane.topology.nodes.publish_event"
_PUBLISH_DELETE = "meho_backplane.topology.node_delete.publish_event"

#: Tools covered by a scenario in this file. Compared against the live
#: registry by :func:`test_every_declaring_tool_has_a_conformance_scenario`.
_COVERED_TOOLS: frozenset[str] = frozenset(
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
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
        headers=headers,
    )


def _assert_conforms(tool_name: str, response: Any) -> dict[str, Any]:
    """Assert a successful tools/call response conforms to the declared schema.

    Returns the ``structuredContent`` payload for scenario-specific
    follow-up assertions. Validation runs with the same pinned
    Draft 2020-12 validator + format checker the input side uses
    (``handlers.handle_tools_call``), so ``format: uuid`` in a declared
    schema is load-bearing here too.
    """
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, body
    result = body["result"]
    assert result["isError"] is False, result
    assert "structuredContent" in result, (
        f"{tool_name}: declaring tool emitted no structuredContent (#2774)"
    )
    structured = result["structuredContent"]
    assert structured == json.loads(result["content"][0]["text"])
    entry = mcp_registry.get_tool(tool_name)
    assert entry is not None, f"missing MCP tool registration: {tool_name!r}"
    schema = entry[0].outputSchema
    assert schema is not None, f"{tool_name}: expected an outputSchema"
    jsonschema.validate(
        instance=structured,
        schema=schema,
        cls=jsonschema.Draft202012Validator,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    return structured


async def _seed_tenant() -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))


async def _seed_node(*, kind: str, name: str, source: str = "curated") -> uuid.UUID:
    node_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=OPERATOR_TENANT_ID,
                kind=kind,
                name=name,
                target_id=None,
                source=source,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
                last_seen=datetime.now(UTC),
            ),
        )
    return node_id


async def _seed_target(name: str, product: str, host: str) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(
            TargetORM(
                tenant_id=OPERATOR_TENANT_ID,
                name=name,
                aliases=[],
                product=product,
                host=host,
                port=443,
                secret_ref="targets/conformance",
                auth_model="shared_service_account",
            ),
        )


def _custom_operator(
    *,
    role: TenantRole,
    capabilities: frozenset[str] = frozenset(),
    principal_kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub="op-conformance",
        name="Conformance",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
        principal_kind=principal_kind,
    )


@pytest.fixture
def custom_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` bound to an :class:`Operator` built from the param.

    Param is the ``Operator``-kwargs dict for :func:`_custom_operator` —
    the shared ``client_with_operator`` cannot express capabilities or
    an agent ``principal_kind`` (its ``build_operator`` accepts role +
    ``platform_admin`` only), and the doc-collections capability gate
    and the approval-parking dial both hang off those fields.
    """
    op = _custom_operator(**request.param)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


# ---------------------------------------------------------------------------
# Coverage completeness
# ---------------------------------------------------------------------------


def test_every_declaring_tool_has_a_conformance_scenario() -> None:
    """Every outputSchema-declaring tool on the registry is covered here.

    The declaring roster itself is pinned in
    ``tests/test_mcp_structured_content.py``; this guard closes the
    other half — a declaration without a real-payload scenario is an
    untested client-enforced contract, which is exactly how the 12
    original tools shipped hard-failing in Claude Desktop (#2774).
    """
    declaring = {
        defn.name
        for defn, _handler in mcp_registry._TOOLS.values()
        if defn.outputSchema is not None
    }
    uncovered = declaring - _COVERED_TOOLS
    assert not uncovered, (
        f"declaring tool(s) {sorted(uncovered)} have no payload-conformance "
        "scenario: add one to this file and extend _COVERED_TOOLS"
    )
    stale = _COVERED_TOOLS - declaring
    assert not stale, (
        f"covered tool(s) {sorted(stale)} no longer declare an outputSchema: "
        "drop their scenarios and shrink _COVERED_TOOLS"
    )


# ---------------------------------------------------------------------------
# Audit family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_audit_flat_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Flat filter path: real substrate query → ``{rows, next_cursor}``."""
    client, _op = client_with_operator
    payload = _assert_conforms("query_audit", _call(client, "query_audit", {}))
    assert payload["rows"] == []
    assert payload["next_cursor"] is None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_audit_tree_shape_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``shape="tree"`` returns the replay envelope — the #2774 drift case.

    The pre-#2774 declaration required ``{rows, next_cursor}``
    unconditionally, so every tree-shaped result violated the published
    contract; the widened ``oneOf`` (flat | replay) is validated here
    against a real self-session replay.
    """
    client, _op = client_with_operator
    session_id = str(uuid.uuid4())
    response = _call(
        client,
        "query_audit",
        {"shape": "tree", "agent_session_id": session_id},
        headers={"Mcp-Session-Id": session_id},
    )
    payload = _assert_conforms("query_audit", response)
    assert payload["session_id"] == session_id
    assert payload["root"] == []
    assert payload["row_count"] == 0


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_meho_audit_replay_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, op = client_with_operator
    session_id = str(uuid.uuid4())
    payload = _assert_conforms(
        "meho_audit_replay",
        _call(client, "meho_audit_replay", {"session_id": session_id}),
    )
    assert payload["tenant_id"] == str(op.tenant_id)
    assert payload["row_count"] == 0


# ---------------------------------------------------------------------------
# Doc-collections family (capability-gated)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "custom_client",
    [
        {
            "role": TenantRole.OPERATOR,
            "capabilities": frozenset({_DOCS_CAPABILITY, "meho-docs:vmware"}),
        }
    ],
    indirect=True,
)
async def test_list_doc_collections_conforms(
    custom_client: tuple[TestClient, Operator],
) -> None:
    client, _op = custom_client
    await seed_doc_collection(collection_key="vmware")
    payload = _assert_conforms("list_doc_collections", _call(client, "list_doc_collections", {}))
    assert [c["collection_key"] for c in payload["collections"]] == ["vmware"]


@pytest.mark.parametrize(
    "custom_client",
    [
        {
            "role": TenantRole.TENANT_ADMIN,
            "capabilities": frozenset({_DOCS_CAPABILITY}),
        }
    ],
    indirect=True,
)
async def test_create_doc_collections_conforms(
    custom_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = custom_client
    payload = _assert_conforms(
        "create_doc_collections",
        _call(
            client,
            "create_doc_collections",
            {
                "collection_key": "vmware",
                "vendor": "VMware by Broadcom",
                "products": ["vsphere", "nsx"],
                "backend": {
                    "type": "corpus-http",
                    "ref": {"endpoint": "https://corpus.test/v1/search"},
                },
            },
        ),
    )
    assert payload["collection_key"] == "vmware"
    assert payload["status"] == "provisioning"


@pytest.mark.parametrize(
    "custom_client",
    [
        {
            "role": TenantRole.TENANT_ADMIN,
            "capabilities": frozenset({_DOCS_CAPABILITY}),
        }
    ],
    indirect=True,
)
async def test_delete_doc_collections_conforms(
    custom_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = custom_client
    await seed_doc_collection(
        collection_key="vmware", status="disabled", tenant_id=OPERATOR_TENANT_ID
    )
    payload = _assert_conforms(
        "delete_doc_collections",
        _call(client, "delete_doc_collections", {"collection_key": "vmware"}),
    )
    assert payload == {"collection_key": "vmware"}


# ---------------------------------------------------------------------------
# JSONFlux read-back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_result_query_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Window read-back over a stubbed store; handler shaping is real."""
    client, _op = client_with_operator
    handle_id = uuid.uuid4()
    rows = [{"i": i} for i in range(8)]

    class _StubStore:
        async def fetch_window(
            self,
            *,
            tenant_id: uuid.UUID,
            operator_sub: str,
            handle_id: uuid.UUID,
            offset: int,
            limit: int,
        ) -> SpilledWindow:
            window = rows[offset : offset + limit]
            return SpilledWindow(
                rows=window,
                total_rows=len(rows),
                stored_rows=len(rows),
                truncated=False,
            )

    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: _StubStore())
    payload = _assert_conforms(
        "result_query",
        _call(client, "result_query", {"handle_id": str(handle_id), "limit": 5}),
    )
    assert payload["returned_rows"] == 5
    assert payload["total_rows"] == 8


# ---------------------------------------------------------------------------
# Operations family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_list_operation_groups_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Real listing of the lifespan-registered vault connector's groups."""
    client, _op = client_with_operator
    payload = _assert_conforms(
        "list_operation_groups",
        _call(client, "list_operation_groups", {"connector_id": "vault-1.x"}),
    )
    assert any(g["group_key"] == "kv" for g in payload["groups"])


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_search_operations_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real hybrid search over the lifespan-registered vault ops."""
    client, _op = client_with_operator
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    async def _fake_get_embedding_service() -> Any:
        return service

    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        _fake_get_embedding_service,
    )
    payload = _assert_conforms(
        "search_operations",
        _call(
            client,
            "search_operations",
            {"connector_id": "vault-1.x", "query": "read a secret"},
        ),
    )
    assert isinstance(payload["hits"], list)


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_call_operation_ok_envelope_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Real end-to-end dispatch: a governed topology write through
    ``call_operation`` returns the full ``OperationResult`` envelope
    (including the always-present ``handle`` key the pre-#2774 schema
    omitted)."""
    client, _op = client_with_operator
    with patch(_PUBLISH_NODES, new=AsyncMock()):
        response = _call(
            client,
            "call_operation",
            {
                "connector_id": "topology-graph-1.x",
                "op_id": "topology.create_node",
                "params": {"kind": "vault-role", "name": "conf-call-op"},
            },
        )
    payload = _assert_conforms("call_operation", response)
    assert payload["status"] == "ok", payload
    assert "handle" in payload


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_preview_operation_unavailable_envelope_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A typed op has no literal HTTP request: ``status="unavailable"``.

    A first-class non-ok envelope — exactly the kind of result shape a
    client-side validator rejects when the schema only imagined the
    happy path.
    """
    client, _op = client_with_operator
    payload = _assert_conforms(
        "preview_operation",
        _call(
            client,
            "preview_operation",
            {"connector_id": "topology-graph-1.x", "op_id": "topology.create_node"},
        ),
    )
    assert payload["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Topology reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_untracked_closure_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Closure kind on an untracked anchor: the ``node_untracked`` envelope."""
    client, _op = client_with_operator
    payload = _assert_conforms(
        "query_topology",
        _call(client, "query_topology", {"kind": "dependents", "target": "ghost"}),
    )
    assert payload["status"] == "node_untracked"
    assert payload["nodes"] == []


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_query_topology_edges_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Edges facet: the ``{kind, edges}`` envelope with a real edge row.

    The ``list_edges`` substrate itself is PG-only (its window SQL
    raises ``OperationalError`` on the SQLite test DB — the existing
    topology suite patches it for the same reason), so the substrate is
    stubbed at the tool's import site with a real
    :class:`~meho_backplane.topology.schemas.TopologyEdge`; the
    handler's ``model_dump`` shaping is what this scenario pins.
    """
    client, _op = client_with_operator
    edge = TopologyEdge(
        id=uuid.uuid4(),
        from_endpoint=TopologyEdgeEndpoint(id=uuid.uuid4(), kind="service", name="svc-x"),
        to_endpoint=TopologyEdgeEndpoint(id=uuid.uuid4(), kind="database", name="db-y"),
        kind="depends-on",
        source="curated",
        properties={},
        last_seen=datetime.now(UTC),
    )
    with patch(
        "meho_backplane.mcp.tools.topology.list_edges",
        new=AsyncMock(return_value=[edge]),
    ):
        payload = _assert_conforms(
            "query_topology", _call(client, "query_topology", {"kind": "edges"})
        )
    assert payload["kind"] == "edges"
    assert payload["edges"][0]["kind"] == "depends-on"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_list_targets_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_tenant()
    await _seed_target("rdc-vault", "vault", "vault.example")
    await _seed_target("rdc-vcenter", "vmware", "vc.example")
    payload = _assert_conforms("list_targets", _call(client, "list_targets", {}))
    assert [t["name"] for t in payload["targets"]] == ["rdc-vault", "rdc-vcenter"]
    assert payload["next_cursor"] is None


# ---------------------------------------------------------------------------
# Topology writes (executed branch — human tenant_admin)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_topology_annotate_and_unannotate_conform(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_tenant()
    await _seed_node(kind="service", name="svc-x")
    await _seed_node(kind="database", name="db-y")
    with patch(_PUBLISH_ANNOTATE, new=AsyncMock()):
        annotated = _assert_conforms(
            "meho_topology_annotate",
            _call(
                client,
                "meho_topology_annotate",
                {"from_name": "svc-x", "kind": "depends-on", "to_name": "db-y"},
            ),
        )
        assert annotated["source"] == "curated"
        removed = _assert_conforms(
            "meho_topology_unannotate",
            _call(
                client,
                "meho_topology_unannotate",
                {"edge_id": annotated["edge_id"]},
                req_id=2,
            ),
        )
    assert removed == {"edge_id": annotated["edge_id"]}


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_topology_create_node_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    with patch(_PUBLISH_NODES, new=AsyncMock()):
        payload = _assert_conforms(
            "meho_topology_create_node",
            _call(
                client,
                "meho_topology_create_node",
                {"kind": "vault-role", "name": "conf-node"},
            ),
        )
    assert payload["was_created"] is True
    assert payload["source"] == "curated"


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_topology_delete_node_conforms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    await _seed_tenant()
    node_id = await _seed_node(kind="vault-role", name="doomed", source="curated")
    with patch(_PUBLISH_DELETE, new=AsyncMock()):
        payload = _assert_conforms(
            "meho_topology_delete_node",
            _call(client, "meho_topology_delete_node", {"node_id": str(node_id)}),
        )
    assert payload == {"node_id": str(node_id), "kind": "vault-role", "name": "doomed"}


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_topology_bulk_import_dry_run_and_apply_conform(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Both faces of the tool: the free dry-run plan and the real apply."""
    client, _op = client_with_operator
    await _seed_tenant()
    await _seed_node(kind="service", name="svc-a")
    await _seed_node(kind="database", name="db-b")
    rows = [{"from_name": "svc-a", "kind": "depends-on", "to_name": "db-b"}]

    plan = _assert_conforms(
        "meho_topology_bulk_import",
        _call(client, "meho_topology_bulk_import", {"rows": rows}),
    )
    assert plan["dry_run"] is True
    assert plan["rows"][0]["edge_id"] is None

    with patch(_PUBLISH_ANNOTATE, new=AsyncMock()):
        applied = _assert_conforms(
            "meho_topology_bulk_import",
            _call(
                client,
                "meho_topology_bulk_import",
                {"rows": rows, "dry_run": False},
                req_id=2,
            ),
        )
    assert applied["dry_run"] is False
    assert applied["created"] == 1


# ---------------------------------------------------------------------------
# Topology writes (parked branch — agent principal)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "custom_client",
    [
        {
            "role": TenantRole.TENANT_ADMIN,
            "principal_kind": PrincipalKind.AGENT,
        }
    ],
    indirect=True,
)
async def test_topology_annotate_parked_branch_conforms(
    custom_client: tuple[TestClient, Operator],
) -> None:
    """An agent-principal write parks: the ``awaiting_approval`` oneOf branch.

    The parked envelope is a first-class success result (MCP-wise it is
    ``isError: false``), so it must validate against the declared
    ``oneOf`` union exactly like the executed shape.
    """
    client, _op = custom_client
    await _seed_tenant()
    await _seed_node(kind="service", name="svc-x")
    await _seed_node(kind="database", name="db-y")
    payload = _assert_conforms(
        "meho_topology_annotate",
        _call(
            client,
            "meho_topology_annotate",
            {"from_name": "svc-x", "kind": "depends-on", "to_name": "db-y"},
        ),
    )
    assert payload["status"] == "awaiting_approval"
    assert uuid.UUID(payload["approval_request_id"])
