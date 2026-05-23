# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G9.2-T7 (#598) admin meta-tools + edges facet.

Three surfaces land here:

* ``meho.topology.annotate`` — tenant_admin-only write meta-tool.
  Calls :func:`annotate_edge` (#595) directly; one audit row + one
  broadcast event per call (the service primitive owns both — the
  dispatcher's own audit row at ``/mcp/tools/call/...`` is a second,
  per-call row on a different ``method`` axis).
* ``meho.topology.unannotate`` — tenant_admin-only write meta-tool.
  Same direct-substrate shape; refuses an `source='auto'` row with a
  structured -32602 (the auto-vs-curated rule).
* ``query_topology(kind="edges", ...)`` — the inventory-survey facet
  on the existing parametric meta-tool. Calls :func:`list_edges`
  (#596) directly; reuses the substrate's `(last_seen DESC NULLS LAST,
  id)` order and the closed v0.2 edge-kind vocabulary filter.

Coverage maps to the issue acceptance criteria:

* Both admin tools register with ``required_role=TENANT_ADMIN`` and
  ``op_class='write'``; a non-admin session does not see them in
  ``tools/list``; a direct ``tools/call`` from a non-admin returns
  -32602 with a ``forbidden``-prefixed message.
* ``tools/call meho.topology.annotate {...}`` creates a curated edge
  (idempotent on repeat) and emits one audit row + one broadcast event.
* ``query_topology {kind: edges, source: curated}`` returns only the
  tenant's curated edges; ``conflicts: true`` narrows to conflicted
  ones; no separate ``list_edges`` tool was registered.
* ``meho.topology.unannotate`` on an auto edge returns a structured
  error mentioning the auto-vs-curated rule.
* Tool descriptions name the when-to-call use case + warn against
  annotating auto-discoverable kinds (the AI-engineering anchor).

The G9.2-T3 service is exercised at the substrate level by
:mod:`tests.test_topology_annotate`; this module's annotate / unannotate
happy-path tests run against the SQLite-migrated test DB so the
end-to-end MCP → service path is covered. The `kind="edges"` dispatch
patches :func:`list_edges` at the tool's import site to keep the test
focused on the MCP dispatch shape (the substrate is covered by
:mod:`tests.integration.test_topology_list_edges`).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, GraphEdge, GraphNode, Tenant
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.topology.schemas import TopologyEdge, TopologyEdgeEndpoint
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# Patch sites — `from meho_backplane.topology... import <symbol>` rebinds
# the symbol into ``mcp.tools.topology``'s module dict, so the local
# name is the patchable reference.
_LIST_EDGES_PATCH = "meho_backplane.mcp.tools.topology.list_edges"
_QUERY_HISTORY_PATCH = "meho_backplane.mcp.tools.topology.query_history"
_PUBLISH_PATCH = "meho_backplane.topology.annotate.publish_event"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    """Insert the operator's :class:`Tenant` row so endpoint resolution finds it.

    The autouse ``_default_database_url`` fixture in :mod:`tests.conftest`
    migrates the schema; we own the tenant row insert.
    """
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


async def _seed_node(*, kind: str, name: str) -> uuid.UUID:
    """Insert one ``graph_node`` row in the operator's tenant; return its id."""
    sessionmaker = get_sessionmaker()
    node_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            GraphNode(
                id=node_id,
                tenant_id=OPERATOR_TENANT_ID,
                kind=kind,
                name=name,
                target_id=None,
                properties={},
                discovered_by="test",
                first_seen=datetime.now(UTC),
            )
        )
        await session.commit()
    return node_id


async def _seed_auto_edge(
    *,
    from_id: uuid.UUID,
    to_id: uuid.UUID,
    kind: str,
) -> uuid.UUID:
    """Insert one ``source='auto'`` ``graph_edge`` row."""
    sessionmaker = get_sessionmaker()
    edge_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        session.add(
            GraphEdge(
                id=edge_id,
                tenant_id=OPERATOR_TENANT_ID,
                from_node_id=from_id,
                to_node_id=to_id,
                kind=kind,
                source="auto",
                properties={},
                discovered_by="test",
                first_seen=now,
                last_seen=now,
            )
        )
        await session.commit()
    return edge_id


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


def _unannotate_call(client: TestClient, call_id: int, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": call_id,
            "method": "tools/call",
            "params": {"name": "meho.topology.unannotate", "arguments": arguments},
        },
    )


def _query_topology_call(
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
            "params": {"name": "query_topology", "arguments": arguments},
        },
    )


def _make_edge(
    *,
    edge_id: UUID | None = None,
    from_name: str = "svc-x",
    from_kind: str = "service",
    to_name: str = "db-y",
    to_kind: str = "database",
    kind: str = "depends-on",
    source: str = "curated",
    properties: dict[str, Any] | None = None,
) -> TopologyEdge:
    return TopologyEdge(
        id=edge_id or uuid.uuid4(),
        from_endpoint=TopologyEdgeEndpoint(id=uuid.uuid4(), kind=from_kind, name=from_name),
        to_endpoint=TopologyEdgeEndpoint(id=uuid.uuid4(), kind=to_kind, name=to_name),
        kind=kind,
        source=source,
        properties=properties or {},
        last_seen=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Registration + narrow-waist
# ---------------------------------------------------------------------------


def test_admin_tools_register_with_tenant_admin_and_write() -> None:
    """Both admin tools land with TENANT_ADMIN gate + write op_class."""
    for tool_name in ("meho.topology.annotate", "meho.topology.unannotate"):
        entry = get_tool(tool_name)
        assert entry is not None, f"{tool_name} not registered"
        defn, _handler = entry
        assert defn.required_role == TenantRole.TENANT_ADMIN
        assert defn.op_class == "write"


def test_no_separate_list_edges_tool_registered() -> None:
    """``list_edges`` is a facet on ``query_topology``, not its own tool.

    Initiative #364 §9 / CLAUDE.md postulate 5: the inventory survey
    collapses into the parametric meta-tool. A standalone ``list_edges``
    tool would re-introduce the per-verb anti-pattern.
    """
    assert get_tool("list_edges") is None


def test_query_topology_input_schema_includes_edges_facet() -> None:
    """The kind enum widened to include 'edges' / 'timeline' / 'diff' / 'history'.

    G9.2-T7 (#598) added ``edges``; G9.3-T5 (#861) added ``timeline``
    (no required field); G9.3-T4 (#860) added ``diff`` (requires both
    timestamps ``ts1`` + ``ts2``); G9.3-T3 (#859) added ``history``
    (requires ``target`` -- the anchor node name). ``edges`` and
    ``timeline`` have no conditional required clause; every filter on
    those two facets is optional.
    """
    entry = get_tool("query_topology")
    assert entry is not None
    defn, _ = entry
    schema = defn.inputSchema
    assert schema["properties"]["kind"]["enum"] == [
        "dependents",
        "dependencies",
        "path",
        "edges",
        "timeline",
        "diff",
        "history",
    ]
    # `diff` requires `ts1` + `ts2`; `history` requires `target`;
    # `edges` and `timeline` have no required field. The other three
    # branches stay as-is. The schema also carries per-kind
    # ``limit.maximum`` tightening clauses for `edges` and `timeline`
    # (intersecting the base permissive ceiling so MCP callers can't
    # smuggle an over-cap value past the schema and trip the
    # substrate's ``ValueError``); those clauses don't carry a
    # ``required`` key, so the ``required``-only dict below skips them
    # via ``.get`` rather than throwing on missing keys.
    by_kind = {
        c["if"]["properties"]["kind"]["const"]: c["then"]["required"]
        for c in schema["allOf"]
        if "required" in c["then"]
    }
    assert "edges" not in by_kind
    assert "timeline" not in by_kind
    assert sorted(by_kind["diff"]) == ["ts1", "ts2"]
    assert by_kind["dependents"] == ["target"]
    assert by_kind["dependencies"] == ["target"]
    assert by_kind["history"] == ["target"]
    # The new filter knobs surface as optional properties on the schema.
    for prop in ("source", "conflicts", "limit", "offset"):
        assert prop in schema["properties"]
    # Timeline knobs.
    for prop in ("since", "until", "cursor"):
        assert prop in schema["properties"]


def test_query_topology_output_schema_widened_for_edges() -> None:
    """The outputSchema names the 'edges' and 'timeline' facets."""
    entry = get_tool("query_topology")
    assert entry is not None
    defn, _ = entry
    out = defn.outputSchema
    assert out is not None
    assert "edges" in out["properties"]
    assert "edges" in out["properties"]["kind"]["enum"]
    # G9.3-T5 (#861): timeline facet adds `rows` + `next_cursor`.
    assert "rows" in out["properties"]
    assert "next_cursor" in out["properties"]
    assert "timeline" in out["properties"]["kind"]["enum"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_admin_tools_hidden_from_non_admin_tools_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Operator-role session does not see the admin tools in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho.topology.annotate" not in names
    assert "meho.topology.unannotate" not in names
    # Read-half tools stay visible.
    assert "query_topology" in names
    assert "list_targets" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_admin_tools_visible_to_tenant_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """tenant_admin session sees both admin tools in ``tools/list``."""
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "meho.topology.annotate" in names
    assert "meho.topology.unannotate" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "meho.topology.annotate",
            {"from_name": "svc-x", "kind": "depends-on", "to_name": "db-y"},
        ),
        (
            "meho.topology.unannotate",
            {"from_name": "svc-x", "kind": "depends-on", "to_name": "db-y"},
        ),
    ],
    ids=["annotate", "unannotate"],
)
def test_admin_tool_call_from_non_admin_is_forbidden(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """tools/call on either admin tool from an operator session → -32602 forbidden.

    Both admin tools share the dispatcher RBAC gate
    (``required_role=TENANT_ADMIN``); the forbidden refusal must fire
    before the inputSchema is even consulted, so the rejection is
    independent of selector shape.
    """
    client, _op = client_with_operator
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# meho.topology.annotate — end-to-end via the SQLite test DB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_annotate_creates_curated_edge_and_emits_audit_plus_broadcast(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """tools/call meho.topology.annotate {...} → curated row + 1 audit + 1 broadcast.

    The dispatcher writes its own audit row at
    ``/mcp/tools/call/meho.topology.annotate`` (the per-MCP-call axis),
    and ``annotate_edge`` writes a second row keyed
    ``op_id='topology.annotate'`` (the service-level axis). The
    issue's "one audit row + one broadcast event" applies to the
    service emission — verified here.
    """
    client, _op = client_with_operator
    await _seed_node(kind="principal", name="k8s-sa-foo")
    await _seed_node(kind="vault-role", name="vault-role-bar")

    with patch(_PUBLISH_PATCH, new=AsyncMock()) as publish_mock:
        response = _annotate_call(
            client,
            10,
            {
                "from_name": "k8s-sa-foo",
                "from_node_kind": "principal",
                "kind": "authenticates-via",
                "to_name": "vault-role-bar",
                "to_node_kind": "vault-role",
                "note": "canonical k8s SA → Vault role",
                "evidence_url": "https://example.test/inventory#L42",
            },
        )

    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["from"]["name"] == "k8s-sa-foo"
    assert payload["to"]["name"] == "vault-role-bar"
    assert payload["kind"] == "authenticates-via"
    assert payload["source"] == "curated"
    assert payload["conflicts"] == []
    assert uuid.UUID(payload["edge_id"])  # valid UUID

    # One curated row in the tenant — the service primitive owns the upsert.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        edges = (
            (
                await session.execute(
                    select(GraphEdge).where(GraphEdge.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )

    assert len(edges) == 1
    assert edges[0].source == "curated"
    assert edges[0].properties["note"] == "canonical k8s SA → Vault role"

    # Exactly one service-level audit row + one MCP dispatcher row.
    service_rows = [a for a in audits if a.path == "topology.annotate"]
    dispatcher_rows = [a for a in audits if a.path == "/mcp/tools/call/meho.topology.annotate"]
    assert len(service_rows) == 1
    assert service_rows[0].method == "ANNOTATE"
    assert len(dispatcher_rows) == 1

    # Exactly one broadcast event (the service-level emission).
    assert publish_mock.await_count == 1


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_annotate_is_idempotent_on_repeat(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """A second annotate of the same triple refreshes properties, not duplicates."""
    client, _op = client_with_operator
    fid = await _seed_node(kind="service", name="svc-x")
    tid = await _seed_node(kind="pod", name="db-y")
    assert fid is not None and tid is not None

    with patch(_PUBLISH_PATCH, new=AsyncMock()):
        first = _annotate_call(
            client,
            20,
            {
                "from_name": "svc-x",
                "from_node_kind": "service",
                "kind": "depends-on",
                "to_name": "db-y",
                "to_node_kind": "pod",
                "note": "first",
            },
        )
        second = _annotate_call(
            client,
            21,
            {
                "from_name": "svc-x",
                "from_node_kind": "service",
                "kind": "depends-on",
                "to_name": "db-y",
                "to_node_kind": "pod",
                "note": "second",
            },
        )

    assert first.json().get("result"), f"first failed: {first.json()}"
    assert second.json().get("result"), f"second failed: {second.json()}"
    first_id = json.loads(first.json()["result"]["content"][0]["text"])["edge_id"]
    second_id = json.loads(second.json()["result"]["content"][0]["text"])["edge_id"]
    assert first_id == second_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = (
            (
                await session.execute(
                    select(GraphEdge).where(GraphEdge.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].properties["note"] == "second"


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_annotate_rejects_unknown_kind_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A made-up `kind` value fails the inputSchema enum (-32602)."""
    client, _op = client_with_operator
    response = _annotate_call(
        client,
        30,
        {
            "from_name": "a",
            "kind": "made-up-kind",
            "to_name": "b",
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_annotate_rejects_additional_properties(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A smuggled `tenant_id` is rejected at the schema layer."""
    client, _op = client_with_operator
    response = _annotate_call(
        client,
        31,
        {
            "from_name": "a",
            "kind": "depends-on",
            "to_name": "b",
            "tenant_id": "00000000-0000-0000-0000-000000000099",
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# meho.topology.unannotate — auto-edge refusal + happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_unannotate_auto_edge_returns_structured_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """Unannotate on an `source='auto'` row → -32602 with auto-discovered message."""
    client, _op = client_with_operator
    fid = await _seed_node(kind="vm", name="vm-a")
    tid = await _seed_node(kind="host", name="host-x")
    await _seed_auto_edge(from_id=fid, to_id=tid, kind="runs-on")

    response = _unannotate_call(
        client,
        40,
        {
            "from_name": "vm-a",
            "from_node_kind": "vm",
            "kind": "runs-on",
            "to_name": "host-x",
            "to_node_kind": "host",
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "auto-discovered" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_unannotate_curated_edge_via_triple(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    """Triple-form unannotate of a curated edge removes the row."""
    client, _op = client_with_operator
    await _seed_node(kind="service", name="svc-x")
    await _seed_node(kind="pod", name="db-y")

    with patch(_PUBLISH_PATCH, new=AsyncMock()):
        created = _annotate_call(
            client,
            50,
            {
                "from_name": "svc-x",
                "from_node_kind": "service",
                "kind": "depends-on",
                "to_name": "db-y",
                "to_node_kind": "pod",
            },
        )
        edge_id = json.loads(created.json()["result"]["content"][0]["text"])["edge_id"]
        removed = _unannotate_call(
            client,
            51,
            {
                "from_name": "svc-x",
                "from_node_kind": "service",
                "kind": "depends-on",
                "to_name": "db-y",
                "to_node_kind": "pod",
            },
        )

    body = removed.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["edge_id"] == edge_id

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = (
            (
                await session.execute(
                    select(GraphEdge).where(GraphEdge.tenant_id == OPERATOR_TENANT_ID)
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_unannotate_rejects_both_selectors_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Passing both ``edge_id`` and the triple → -32602 at the schema layer.

    The substrate-level :class:`UnannotateSelectorError` still guards
    direct in-process callers (and is asserted by
    :mod:`tests.test_topology_annotate`); over the MCP wire the
    rejection now fires at the inputSchema ``oneOf`` before any handler
    runs.
    """
    client, _op = client_with_operator
    response = _unannotate_call(
        client,
        60,
        {
            "edge_id": "00000000-0000-0000-0000-000000000001",
            "from_name": "a",
            "kind": "depends-on",
            "to_name": "b",
        },
    )
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputSchema" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_unannotate_rejects_empty_arguments_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An empty ``{}`` argument bag → -32602 (neither selector form)."""
    client, _op = client_with_operator
    response = _unannotate_call(client, 62, {})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputSchema" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.parametrize(
    "partial_triple",
    [
        # `from_name` missing — the other two triple keys present.
        {"kind": "depends-on", "to_name": "b"},
        # `kind` missing.
        {"from_name": "a", "to_name": "b"},
        # `to_name` missing.
        {"from_name": "a", "kind": "depends-on"},
    ],
    ids=["missing-from_name", "missing-kind", "missing-to_name"],
)
def test_unannotate_rejects_partial_triple_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    partial_triple: dict[str, Any],
) -> None:
    """A triple with one of (from_name, kind, to_name) missing → -32602."""
    client, _op = client_with_operator
    response = _unannotate_call(client, 63, partial_triple)
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputSchema" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.parametrize(
    "empty_field",
    ["edge_id", "from_name", "to_name"],
)
def test_unannotate_rejects_empty_string_selector_at_schema_layer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    empty_field: str,
) -> None:
    """Empty strings in any selector slot → -32602 (``minLength: 1`` enforced).

    A ``""`` field is structurally different from omitted: it would
    satisfy the ``oneOf`` ``required`` check on its own. Without
    ``minLength: 1`` the empty value would slip through the schema and
    only fail at the substrate (or not fail at all if the substrate
    didn't notice). The schema now rejects it at the front.
    """
    client, _op = client_with_operator
    # Build a valid base shape for whichever selector form contains the
    # empty field, then overwrite the targeted field with the empty
    # string.
    if empty_field == "edge_id":
        arguments: dict[str, Any] = {"edge_id": ""}
    else:
        arguments = {"from_name": "a", "kind": "depends-on", "to_name": "b"}
        arguments[empty_field] = ""
    response = _unannotate_call(client, 64, arguments)
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputSchema" in body["error"]["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
def test_unannotate_rejects_malformed_edge_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A non-UUID edge_id is caught at the front before service dispatch.

    The inputSchema doesn't constrain the UUID *format* — the handler
    still owns the ``uuid.UUID(...)`` parse and surfaces -32602 with a
    pointed "not a valid UUID" message. The schema's ``minLength: 1``
    is satisfied by the non-empty garbage string.
    """
    client, _op = client_with_operator
    response = _unannotate_call(client, 61, {"edge_id": "not-a-uuid"})
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "valid UUID" in body["error"]["message"]


# ---------------------------------------------------------------------------
# query_topology(kind=edges) facet — dispatch + filter pass-through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_edges_facet_forwards_source_curated_filter(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=edges + source=curated dispatches to list_edges with the filter."""
    client, op = client_with_operator
    edges = [_make_edge(source="curated")]
    mock_list = AsyncMock(return_value=edges)
    with patch(_LIST_EDGES_PATCH, new=mock_list):
        response = _query_topology_call(
            client,
            70,
            {"kind": "edges", "source": "curated"},
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["kind"] == "edges"
    assert len(payload["edges"]) == 1
    assert payload["edges"][0]["source"] == "curated"

    # Tenant scope comes from the operator — `source` flows through.
    call_kwargs = mock_list.await_args.kwargs
    assert call_kwargs["source"] == "curated"
    assert call_kwargs["conflicts_only"] is False
    # tenant_id is positional arg 1 (positional[0] is the session).
    assert mock_list.await_args.args[1] == op.tenant_id


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_edges_facet_conflicts_only_filter(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind=edges + conflicts=true narrows to conflict-marked edges."""
    client, _op = client_with_operator
    mock_list = AsyncMock(return_value=[])
    with patch(_LIST_EDGES_PATCH, new=mock_list):
        response = _query_topology_call(
            client,
            71,
            {"kind": "edges", "conflicts": True},
        )
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload == {"kind": "edges", "edges": []}
    assert mock_list.await_args.kwargs["conflicts_only"] is True


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_edges_facet_forwards_kind_filter_and_endpoints(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """kind_filter / from_name / to_name / limit / offset all flow through."""
    client, _op = client_with_operator
    mock_list = AsyncMock(return_value=[])
    with patch(_LIST_EDGES_PATCH, new=mock_list):
        _query_topology_call(
            client,
            72,
            {
                "kind": "edges",
                "kind_filter": "depends-on",
                "from_name": "svc-x",
                "to_name": "db-y",
                "limit": 50,
                "offset": 100,
            },
        )
    kwargs = mock_list.await_args.kwargs
    assert kwargs["kind"] == "depends-on"
    assert kwargs["from_ref"] == "svc-x"
    assert kwargs["to_ref"] == "db-y"
    assert kwargs["limit"] == 50
    assert kwargs["offset"] == 100


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_edges_facet_read_role_unchanged(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The edges facet stays on the operator-read gate (no privilege creep).

    The admin meta-tools are the write half; the inventory survey is a
    read shape and stays at ``required_role=OPERATOR`` so an
    ``operator``-role session can call it.
    """
    client, _op = client_with_operator
    mock_list = AsyncMock(return_value=[])
    with patch(_LIST_EDGES_PATCH, new=mock_list):
        response = _query_topology_call(client, 73, {"kind": "edges"})
    assert response.json()["result"]["isError"] is False


# ---------------------------------------------------------------------------
# query_topology(kind=history) facet — limit forwarding (B3 on PR #936)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_history_facet_forwards_limit_to_query_history(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``kind=history`` + caller-supplied ``limit`` flows through to ``query_history``.

    Regression for PR #936 iter-1 finding B3: the dispatcher was
    extracting every other knob (``node_kind`` / ``since`` / ``until`` /
    ``include_edges``) from the arguments dict but silently dropped
    ``limit``, so MCP callers always got the substrate's default
    ceiling (5000) regardless of what they asked for.
    """
    from meho_backplane.topology.schemas import TopologyHistoryResult

    client, _op = client_with_operator
    mock_history = AsyncMock(
        return_value=TopologyHistoryResult(
            anchor_node_id=uuid.uuid4(),
            include_edges=False,
            rows=(),
        )
    )
    with patch(_QUERY_HISTORY_PATCH, new=mock_history):
        response = _query_topology_call(
            client,
            80,
            {"kind": "history", "target": "vm-a", "limit": 250},
        )
    assert response.json()["result"]["isError"] is False
    # ``limit`` rides the kwargs the dispatcher passes -- the substrate
    # validates the 1..5000 range itself, so the MCP layer only forwards.
    kwargs = mock_history.await_args.kwargs
    assert kwargs["limit"] == 250


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_history_facet_defaults_limit_to_history_ceiling(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``kind=history`` with no ``limit`` falls back to the per-facet ceiling.

    Per-resource history is bounded by retention; the default IS the
    ceiling. A tighter default would silently truncate the walk and
    the operator would think they see the full history when they
    don't (same reasoning the REST and CLI fronts use).
    """
    from meho_backplane.mcp.tools.topology import _HISTORY_LIMIT_MAX
    from meho_backplane.topology.schemas import TopologyHistoryResult

    client, _op = client_with_operator
    mock_history = AsyncMock(
        return_value=TopologyHistoryResult(
            anchor_node_id=uuid.uuid4(),
            include_edges=False,
            rows=(),
        )
    )
    with patch(_QUERY_HISTORY_PATCH, new=mock_history):
        _query_topology_call(client, 81, {"kind": "history", "target": "vm-a"})
    kwargs = mock_history.await_args.kwargs
    assert kwargs["limit"] == _HISTORY_LIMIT_MAX


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_history_facet_schema_admits_limit_up_to_history_ceiling(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The MCP schema admits ``limit`` in [1, 5000] for ``kind=history``.

    Regression for PR #936 iter-1 finding M1: the shared ``limit``
    property used to cap at ``_EDGES_LIMIT_MAX`` (1000), schema-rejecting
    valid history calls. The base ceiling now matches
    ``_HISTORY_LIMIT_MAX``; per-facet ``allOf`` clauses tighten ``edges``
    / ``timeline`` back down to 1000 so they can't accidentally widen.
    """
    from meho_backplane.topology.schemas import TopologyHistoryResult

    client, _op = client_with_operator
    mock_history = AsyncMock(
        return_value=TopologyHistoryResult(
            anchor_node_id=uuid.uuid4(),
            include_edges=False,
            rows=(),
        )
    )
    with patch(_QUERY_HISTORY_PATCH, new=mock_history):
        response = _query_topology_call(
            client,
            82,
            {"kind": "history", "target": "vm-a", "limit": 4000},
        )
    # No schema rejection -- the call reached the handler and the
    # mocked substrate returned an empty result.
    assert response.json()["result"]["isError"] is False
    assert mock_history.await_args.kwargs["limit"] == 4000


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_edges_facet_schema_rejects_limit_above_edges_ceiling(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The per-facet ``allOf`` clause holds ``edges`` to ``_EDGES_LIMIT_MAX``.

    Companion to the history test above: the base ``limit.maximum`` was
    widened to ``_HISTORY_LIMIT_MAX`` (5000) so history isn't
    schema-rejected, but ``edges`` still substrate-caps at 1000. The
    ``allOf if/then`` clause intersects a stricter ceiling for the
    ``edges`` branch so an MCP caller can't smuggle ``limit=1500`` past
    the schema and trip ``list_edges``'s ``ValueError`` at runtime.
    """
    client, _op = client_with_operator
    mock_list = AsyncMock(return_value=[])
    with patch(_LIST_EDGES_PATCH, new=mock_list):
        response = _query_topology_call(
            client,
            83,
            {"kind": "edges", "limit": 1500},
        )
    body = response.json()
    # ``INVALID_PARAMS`` already imported at module level (line 64).
    assert body["error"]["code"] == INVALID_PARAMS
    # The mocked substrate was never reached -- rejection happened at
    # the schema layer.
    mock_list.assert_not_awaited()


# ---------------------------------------------------------------------------
# Description audits (AI-engineering anchor: name the use case)
# ---------------------------------------------------------------------------


def test_annotate_description_warns_against_auto_kinds() -> None:
    """The annotate description steers operators away from auto-discoverable kinds.

    Initiative #364 / G9.2 narrative: annotating an edge probes already
    discover is noise — the next refresh will mark the assertion as a §6
    conflict marker and clutter the inventory. The tool description must
    say so explicitly (the "WHEN TO CALL" + "DO NOT" pair the AI-engineering
    best-practices anchor recommends).
    """
    entry = get_tool("meho.topology.annotate")
    assert entry is not None
    desc = entry[0].description
    assert "WHEN TO CALL" in desc
    assert "DO NOT" in desc
    # Names at least the canonical cross-system kinds.
    assert "authenticates-via" in desc
    assert "tenant_admin" in desc


def test_annotate_description_matches_actual_response_shape() -> None:
    """The annotate description must not advertise response fields the handler
    does not populate (B1 regression on PR #654).

    The handler returns ``edge_id / from / to / kind / source / conflicts``;
    earlier iterations also claimed ``superseded: [<auto-edge-id>...]`` on
    the response shape, but ``annotate_edge`` only stamps that on the
    auto-edge ``properties`` and on the audit/broadcast payload — it
    never surfaces it on the return value. Description-vs-impl drift
    here is load-bearing for an LLM agent reading the description as
    the API contract.
    """
    entry = get_tool("meho.topology.annotate")
    assert entry is not None
    defn = entry[0]
    desc = defn.description
    declared = set(defn.outputSchema.get("properties", {}).keys())
    # outputSchema is the canonical contract — anything the description
    # *names* as a response key must be in it.
    assert declared == {"edge_id", "from", "to", "kind", "source", "conflicts"}
    # Belt-and-suspenders: the literal "Returns `{...}`" clause names
    # only the keys above.
    assert "Returns `{edge_id, from:" in desc
    # `superseded` is the substrate-side §6 marker
    # (`properties.superseded_by` on the displaced auto-edge row +
    # audit payload field), never a response key on this tool. The
    # description may *mention*
    # the mechanism in parenthetical guidance but must not advertise it
    # as a returned key.
    assert "superseded: [<auto-edge-id>...]" not in desc
    assert "superseded_by" in desc  # parenthetical reference to the substrate mechanism remains


def test_unannotate_description_names_auto_refusal() -> None:
    """unannotate description names the auto-vs-curated refusal rule."""
    entry = get_tool("meho.topology.unannotate")
    assert entry is not None
    desc = entry[0].description
    assert "auto" in desc.lower()
    assert "tenant_admin" in desc
