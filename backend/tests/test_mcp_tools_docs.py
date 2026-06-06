# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the collection-scoped ``search_docs`` MCP tool (G4.6-T3, #1552).

Covers the collection-scoped contract layered on the G4.5-T4 capability gate:

* ``search_docs`` is registered with ``required_capability="meho-docs"``;
  it is **absent** from ``tools/list`` for a tenant without the base
  capability and **present** for one with it (T1's gate, #1519).
* ``inputSchema`` is strict (``additionalProperties: false``, required
  ``[query, collection]``, product/version demoted to optional).
* ``tools/call search_docs`` from a tenant lacking the base ``meho-docs``
  capability → 403-class error (the handler never runs); a missing
  ``collection`` → ``-32602``.
* **Per-collection entitlement (#1552):** a tenant with the base
  ``meho-docs`` capability but lacking ``meho-docs:<collection>`` → a
  403-class ``-32602`` even though the tool is visible; with it → the
  query routes to the collection's backend.
* **Collection scope routing:** the query reaches the resolved backend
  with the optional product/version refinements as ``metadata_filters``;
  an unknown collection → ``-32602``, a not-ready collection → ``-32603``.
* The audit row carries the canonical ``op_id="meho.docs.search"`` plus
  ``audit_collection``; the raw query is recorded only as a hash.

The ``corpus-http`` backend wraps the JWT-forward transport; these unit
tests mock
:func:`meho_backplane.docs_search.backends.corpus_http.search_corpus` so
the wire shape + scope plumbing are exercised without a live corpus.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.corpus import CorpusChunk, CorpusSearchResponse, CorpusUnavailable
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seed_doc_collection,
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)

_DOCS_CAPABILITY = "meho-docs"
#: Per-collection entitlement key for the seeded ``vmware`` collection.
_VMWARE_CAP = "meho-docs:vmware"
#: A provisioned + vmware-entitled capability set.
_ENTITLED = frozenset({_DOCS_CAPABILITY, _VMWARE_CAP})

#: The corpus-http backend's transport seam — the function the
#: ``corpus-http`` adapter actually calls. Patching here exercises the
#: full router → backend → transport path.
_CORPUS_SEAM = "meho_backplane.docs_search.backends.corpus_http.search_corpus"


async def _mcp_audit_rows() -> list[AuditLog]:
    """Read every MCP-method ``audit_log`` row, oldest first."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in result.scalars().all() if row.method == "MCP"]


def _seed_collection_sync(**kwargs: Any) -> None:
    """Run :func:`seed_doc_collection` to completion from a sync test.

    ``asyncio.run`` spins a fresh loop for the one-shot DB write; the
    file-backed SQLite engine the autouse ``_default_database_url`` fixture
    pins works across loops, so the row is visible to the subsequent
    ``TestClient`` request (which runs its own loop).
    """
    asyncio.run(seed_doc_collection(**kwargs))


def _operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    capabilities: frozenset[str] = frozenset(),
) -> Operator:
    """Build a fixture operator with the requested role + capability set."""
    return Operator(
        sub="op-test",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=capabilities,
    )


@pytest.fixture
def docs_client(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[TestClient, Operator]]:
    """``TestClient`` whose operator's capability set is parametrised.

    Provision the capability set with
    ``@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)``;
    default is the empty set (unprovisioned).
    """
    capabilities: frozenset[str] = getattr(request, "param", frozenset())
    op = _operator(role=TenantRole.OPERATOR, capabilities=capabilities)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client, op
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _fake_corpus(
    *chunks: CorpusChunk,
) -> Any:
    """An async stand-in for ``search_corpus`` capturing its call args."""
    captured: dict[str, Any] = {}

    async def _search(operator: Operator, query: str, **kwargs: Any) -> CorpusSearchResponse:
        captured["operator"] = operator
        captured["query"] = query
        captured.update(kwargs)
        return CorpusSearchResponse(chunks=list(chunks))

    _search.captured = captured  # type: ignore[attr-defined]
    return _search


_SAMPLE_CHUNK = CorpusChunk(
    chunk_id="nsx-9.0-maximums-0007",
    document_id="nsx-9.0-config-maximums",
    content="NSX 9.0 supports up to 10,000 logical switches per manager.",
    source_url="https://docs.example.com/nsx/9.0/maximums",
    score=0.91,
)


# ---------------------------------------------------------------------------
# tools/list shape + capability gate (visibility)
# ---------------------------------------------------------------------------


def test_search_docs_absent_from_tools_list_for_unprovisioned_tenant(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An operator without the base ``meho-docs`` never sees the tool."""
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "search_docs" not in names


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_search_docs_present_with_strict_collection_schema(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """The tool appears once provisioned, with a strict collection-scoped schema.

    Required ``[query, collection]``, ``additionalProperties: false``,
    product/version demoted to optional, and the MEHO-internal RBAC +
    capability fields stripped by :meth:`ToolDefinition.to_wire`. Tool
    visibility rides only the base ``meho-docs`` capability — the
    per-collection entitlement is enforced at call time, not at list time.
    """
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    assert "search_docs" in tools_by_name
    tool = tools_by_name["search_docs"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["query", "collection"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"query", "collection", "product", "version", "limit"}
    assert schema["properties"]["limit"]["default"] == 10
    assert schema["properties"]["limit"]["maximum"] == 50
    # Wire shape never leaks the server-side gating fields.
    assert "required_role" not in tool
    assert "op_class" not in tool
    assert "required_capability" not in tool


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_search_docs_description_names_collection_and_siblings(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """The description names the collection scope, the sibling tools, and the resource."""
    client, _op = docs_client
    response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}
    desc = tools_by_name["search_docs"]["description"]

    assert "collection" in desc.lower()
    assert "list_doc_collections" in desc
    assert "search_knowledge" in desc
    assert "search_memory" in desc
    # Companion resource pointer now carries the collection segment.
    assert "meho://docs/{collection}/" in desc
    assert "resources/read" in desc


def test_search_docs_hidden_from_provisioned_read_only_operator() -> None:
    """The capability does not relax the role gate: read_only never sees it."""
    op = _operator(role=TenantRole.READ_ONLY, capabilities=_ENTITLED)

    async def _fake_verify() -> Operator:
        return op

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            response = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
    names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "search_docs" not in names


# ---------------------------------------------------------------------------
# tools/call — base capability gate (visibility re-check)
# ---------------------------------------------------------------------------


def test_tools_call_search_docs_403_when_unprovisioned(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """Naming the tool directly still 403s when the base capability is absent."""
    client, _op = docs_client
    with patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)) as spy:
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "nsx maximums", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()
    assert "capability" in body["error"]["message"].lower()
    assert "query" not in spy.captured  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# tools/call — per-collection entitlement (#1552)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [frozenset({_DOCS_CAPABILITY})], indirect=True)
def test_tools_call_search_docs_403_when_not_entitled_to_collection(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A tenant with base ``meho-docs`` but not ``meho-docs:vmware`` → 403-class.

    The tool is visible (base capability present), but the per-collection
    entitlement gate rejects the query before it reaches the backend.
    """
    client, _op = docs_client
    _seed_collection_sync()
    with patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)) as spy:
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "config maximums", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "entitled" in body["error"]["message"].lower()
    assert "query" not in spy.captured  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# tools/call — entitled happy path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_routes_to_collection_backend(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An entitled operator's query routes to the collection's backend with refinements.

    The handler resolves the ``vmware`` collection, routes through the
    ``corpus-http`` backend, and the optional product/version refinements
    reach the transport as ``metadata_filters``. The backend id is absent
    from the response.
    """
    client, op = docs_client
    _seed_collection_sync()
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch(_CORPUS_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {
                        "query": "config maximums",
                        "collection": "vmware",
                        "product": "nsx",
                        "version": "9.0",
                        "limit": 5,
                    },
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert len(payload["chunks"]) == 1
    chunk = payload["chunks"][0]
    assert chunk["chunk_id"] == "nsx-9.0-maximums-0007"
    # The backend id never appears in the response.
    assert "backend" not in json.dumps(payload)

    captured = fake.captured  # type: ignore[attr-defined]
    assert captured["query"] == "config maximums"
    assert captured["limit"] == 5
    assert captured["metadata_filters"] == {"product": "nsx", "version": "9.0"}
    assert captured["operator"].tenant_id == op.tenant_id


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_collection_only_omits_refinements(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """Product/version are optional: a collection-only query still succeeds.

    With no product/version, no ``metadata_filters`` reach the backend
    (the collection alone scopes the query).
    """
    client, _op = docs_client
    _seed_collection_sync()
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch(_CORPUS_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "anything", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    # No product/version → no metadata_filters forwarded.
    assert fake.captured["metadata_filters"] is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# tools/call — collection-scope rejection arms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_missing_collection_rejected_by_schema(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A missing required ``collection`` fails inputSchema validation → -32602."""
    client, _op = docs_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "search_docs",
                "arguments": {"query": "x", "product": "nsx"},
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_unknown_collection_is_invalid_params(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """An unknown collection key → -32602 (invalid argument, not a server fault)."""
    client, _op = docs_client
    # No collection seeded → resolve fails.
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch(_CORPUS_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "x", "collection": "nope"},
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "unknown collection" in body["error"]["message"].lower()
    assert "query" not in fake.captured  # type: ignore[attr-defined]


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_not_ready_collection_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A known + entitled but not-``ready`` collection → -32603 (server-side condition)."""
    client, _op = docs_client
    _seed_collection_sync(status="rebuilding")
    fake = _fake_corpus(_SAMPLE_CHUNK)
    with patch(_CORPUS_SEAM, new=fake):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "x", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR
    assert "query" not in fake.captured  # type: ignore[attr-defined]


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_rejects_extra_arguments(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """``additionalProperties: false`` rejects unknown top-level keys."""
    client, _op = docs_client
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "search_docs",
                "arguments": {
                    "query": "x",
                    "collection": "vmware",
                    "tenant_id": "smuggled",
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# tools/call — backend unavailable surfaces as INTERNAL_ERROR (not -32602)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
def test_tools_call_search_docs_backend_unavailable_is_internal_error(
    docs_client: tuple[TestClient, Operator],
) -> None:
    """A down backend is a server fault → ``-32603``, not invalid params."""
    client, _op = docs_client
    _seed_collection_sync()

    async def _down(_op: Operator, _query: str, **_kwargs: Any) -> CorpusSearchResponse:
        raise CorpusUnavailable("corpus_url is not configured")

    with patch(_CORPUS_SEAM, new=_down):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "x", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Uniform audit op_id + collection across faces (#1549 / #1552)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
async def test_tools_call_search_docs_audit_row_carries_op_id_and_collection(
    docs_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """The MCP audit row's ``op_id`` is canonical and carries ``collection``.

    The handler binds the ``audit_op_id`` and ``audit_collection``
    contextvars; the dispatcher lifts them into the persisted row.
    ``op_class`` stays ``read`` and the raw query is recorded only as a
    hash, never in the clear.
    """
    client, _op = docs_client
    await seed_doc_collection()
    with patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "config maximums", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _mcp_audit_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "meho.docs.search"
    assert payload["op_class"] == "read"
    assert payload["collection"] == "vmware"
    assert "config maximums" not in json.dumps(payload)
    assert payload["params_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("docs_client", [_ENTITLED], indirect=True)
async def test_query_audit_op_id_filter_catches_mcp_search_docs(
    docs_client: tuple[TestClient, Operator],
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    """A ``query_audit`` ``op_id`` filter returns the MCP collection-scoped call."""
    from meho_backplane.audit_query import AuditQueryFilters, query_audit

    client, _op = docs_client
    await seed_doc_collection()
    with patch(_CORPUS_SEAM, new=_fake_corpus(_SAMPLE_CHUNK)):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "search_docs",
                    "arguments": {"query": "nsx maximums", "collection": "vmware"},
                },
            },
        )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await query_audit(
            AuditQueryFilters(op_id="meho.docs.search"),
            tenant_id=OPERATOR_TENANT_ID,
            session=session,
        )
    assert len(result.rows) == 1
    assert result.rows[0].op_id == "meho.docs.search"
    assert result.rows[0].op_class == "read"
