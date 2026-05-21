# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``search_knowledge`` / ``add_to_knowledge`` MCP tools (G4.1-T3, #417).

Covers every acceptance criterion in the task body:

* Both meta-tools are registered against the G0.5 registry; ``tools/list``
  surfaces them with strict 2020-12 ``inputSchema`` (``additionalProperties:
  false``) and the MEHO-internal RBAC fields stripped from the wire shape.
* ``inputSchema`` ``required`` lists match the spec: ``[query]`` for
  ``search_knowledge``; ``[slug, body]`` for ``add_to_knowledge``.
* ``tools/call search_knowledge`` against a seeded corpus returns ranked
  hits adapted from :class:`KbEntrySearchHit`.
* ``tools/call add_to_knowledge`` creates the entry; a follow-up
  ``search_knowledge`` finds it.
* Tenant boundary: a search initiated under tenant A never returns
  tenant B's entries.
* Audit + broadcast emit per call via the shared dispatcher path
  (covered transitively — the dispatcher's own behaviour is asserted
  in :mod:`tests.test_mcp_audit`).

Embedding is mocked the same way :mod:`tests.test_kb_service` mocks it,
so the SQLite-backed default DB carries the suite without needing
fastembed at test time. The retrieve substrate's BM25 + cosine ranking
is exercised in the PG-real integration tests; here the goal is the
**MCP wire shape** plus end-to-end service round-trip.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.service import KbService
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.retrieval.retriever import RetrievalHit
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


@pytest.fixture
def stub_embedding() -> Iterator[AsyncMock]:
    """Patch :func:`get_embedding_service` so the substrate encodes deterministically.

    Mirrors the embedding stub in :mod:`tests.test_kb_service` — the
    indexer's embedding compute is the only path that pulls fastembed
    /ONNX runtime, so patching at the import site lets the SQLite-
    backed test DB carry every assertion below.
    """
    fake = AsyncMock()
    fake.encode_one.return_value = [0.1] * 384
    fake.encode.return_value = [[0.1] * 384]
    fake.dimension = 384
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake,
    ):
        yield fake.encode_one


# ---------------------------------------------------------------------------
# tools/list shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_exposes_kb_tools_with_strict_input_schema(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #1, #2, #3: both tools appear with strict 2020-12 schemas.

    ``search_knowledge`` requires ``[query]``; ``add_to_knowledge``
    requires ``[slug, body]``. Both have ``additionalProperties: false``.
    The MEHO-internal RBAC fields (``required_role`` / ``op_class``)
    are stripped by :meth:`ToolDefinition.to_wire`.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    body = response.json()
    tools_by_name = {t["name"]: t for t in body["result"]["tools"]}

    assert "search_knowledge" in tools_by_name
    search = tools_by_name["search_knowledge"]
    assert search["inputSchema"]["type"] == "object"
    assert search["inputSchema"]["required"] == ["query"]
    assert search["inputSchema"]["additionalProperties"] is False
    assert "query" in search["inputSchema"]["properties"]
    assert "filters" in search["inputSchema"]["properties"]
    assert "limit" in search["inputSchema"]["properties"]
    assert "required_role" not in search
    assert "op_class" not in search

    assert "add_to_knowledge" in tools_by_name
    add = tools_by_name["add_to_knowledge"]
    assert add["inputSchema"]["required"] == ["slug", "body"]
    assert add["inputSchema"]["additionalProperties"] is False
    assert "slug" in add["inputSchema"]["properties"]
    assert "body" in add["inputSchema"]["properties"]
    assert "metadata" in add["inputSchema"]["properties"]
    assert "required_role" not in add
    assert "op_class" not in add


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tool_descriptions_satisfy_ai_engineering_anchor(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #4: descriptions name what, when-to-use, when-NOT-to-use, and slug shape.

    Smoke-asserts the load-bearing pieces of the description prose so a
    future edit that removes them surfaces here rather than as silently
    degraded agent UX. Keeps the assertion loose (substring presence)
    so prose can evolve without churn.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    search_desc = tools_by_name["search_knowledge"]["description"]
    # What: kb search.
    assert "knowledge base" in search_desc.lower()
    # When to use: before adding, before asking.
    assert "BEFORE writing" in search_desc or "before writing" in search_desc.lower()
    # Pointer to the companion resource for the full body.
    assert "meho://kb/" in search_desc

    add_desc = tools_by_name["add_to_knowledge"]["description"]
    # When to use: capturing generalisable knowledge.
    assert "generalizable" in add_desc.lower() or "generalisable" in add_desc.lower()
    # When NOT to use: ephemeral session notes → memory (G5).
    assert "memory" in add_desc.lower()
    # Slug-first discipline: search first.
    assert "search_knowledge" in add_desc


# ---------------------------------------------------------------------------
# search_knowledge — call path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_search_knowledge_returns_ranked_hits(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #6: search returns ranked hits adapted to :class:`KbEntrySearchHit`.

    The retrieve substrate's hybrid SQL ranks against PostgreSQL-only
    ``to_tsvector`` + ``pgvector``; SQLite-backed unit tests can't run
    the real path. Mocking
    :func:`meho_backplane.kb.service.retrieve` (same pattern
    :mod:`tests.test_kb_service` uses for the adapter test) covers
    the MCP wire-shape contract — slug + snippet + fused_score
    round-trip through ``model_dump(mode="json")`` and into the
    dispatcher's ``content[0].text`` JSON-encoded envelope. PG-real
    BM25 + cosine coverage lives in the integration suite.
    """
    client, op = client_with_operator
    captured: dict[str, object] = {}

    ts = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    fake_hit = RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=op.tenant_id,
        source="kb",
        source_id="vsphere-snapshot-revert",
        kind="kb-entry",
        body="How to revert a vSphere VM snapshot via the REST API.",
        doc_metadata={"author": "ops"},
        created_at=ts,
        updated_at=ts,
        fused_score=0.8,
        bm25_score=0.5,
        cosine_score=0.9,
        bm25_rank=1,
        cosine_rank=1,
    )

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return [fake_hit]

    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {"query": "snapshot revert"},
                },
            },
        )

    # Substrate was called with KB source pinned and the operator's tenant.
    assert captured["source"] == "kb"
    assert captured["tenant_id"] == op.tenant_id
    assert captured["query"] == "snapshot revert"

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert "hits" in payload
    assert isinstance(payload["hits"], list)
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    assert hit["slug"] == "vsphere-snapshot-revert"
    assert "snapshot" in hit["snippet"].lower()
    assert hit["fused_score"] == pytest.approx(0.8)
    assert hit["metadata"]["author"] == "ops"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_search_knowledge_forwards_filters_and_limit(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The ``filters`` and ``limit`` arguments reach the retrieve call.

    The handler unwraps ``filters={"kind": "..."}`` into the
    :meth:`KbService.search_entries` call and forwards ``limit``
    verbatim. The fake retrieve captures both so we can assert the
    plumbing without exercising the SQL.
    """
    client, _op = client_with_operator
    captured: dict[str, object] = {}

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return []

    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {
                        "query": "x",
                        "filters": {"kind": "kb-entry"},
                        "limit": 5,
                    },
                },
            },
        )
    assert response.status_code == 200
    assert captured["kind"] == "kb-entry"
    assert captured["limit"] == 5


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_search_knowledge_rejects_missing_query(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC #2: missing required ``query`` fails JSON-Schema validation → -32602."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_knowledge", "arguments": {}},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_search_knowledge_rejects_extra_arguments(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``additionalProperties: false`` rejects unknown top-level keys."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_knowledge",
                "arguments": {"query": "anything", "unknown_field": 42},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# add_to_knowledge — call path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_knowledge_creates_entry_and_is_findable(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC #7: add_to_knowledge creates the row; the entry is findable via service.

    Writes through the MCP transport, then verifies the row landed by
    reading back through :meth:`KbService.get_entry` (SELECT-only —
    portable across SQLite + PG, so this test exercises the full
    write path without depending on the substrate's PG-only retrieval
    SQL). The PG-real round-trip through ``search_entries`` is in the
    integration suite; the wire-shape contract here is the response
    shape from ``add_to_knowledge`` (full :class:`KbEntry`
    ``model_dump(mode="json")``) plus the substrate write.
    """
    client, op = client_with_operator

    create = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_knowledge",
                "arguments": {
                    "slug": "vault-jwt-login",
                    "body": "Vault federation login with JWT auth method.",
                    "metadata": {"source": "agent"},
                },
            },
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["result"]["isError"] is False
    created = json.loads(body["result"]["content"][0]["text"])
    assert created["slug"] == "vault-jwt-login"
    assert created["body"] == "Vault federation login with JWT auth method."
    assert created["metadata"]["source"] == "agent"
    # KbEntry model_dump payload carries the substrate-side id +
    # timestamps; assert their shape so a future field-rename
    # surfaces here.
    assert "id" in created
    assert "created_at" in created
    assert "updated_at" in created

    # The substrate row is actually present and scoped to the operator's tenant.
    service = KbService()
    fetched = await service.get_entry(tenant_id=op.tenant_id, slug="vault-jwt-login")
    assert fetched is not None
    assert fetched.body == "Vault federation login with JWT auth method."
    assert fetched.metadata["source"] == "agent"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_add_to_knowledge_rejects_invalid_slug(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """A slug that fails :data:`SLUG_PATTERN` returns INVALID_PARAMS (-32602).

    The handler's ``InvalidKbSlugError`` → ``McpInvalidParamsError`` map
    is the spec-correct mapping: it's the client's input that's bad,
    not the server's state.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_knowledge",
                "arguments": {
                    "slug": "Bad_Slug_With_Underscores",
                    "body": "Body text.",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "slug" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_add_to_knowledge_rejects_missing_required(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Schema validation fires before the handler — missing ``body`` → -32602."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_knowledge",
                "arguments": {"slug": "ok-slug"},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# RBAC visibility — read_only operator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_kb_tools_hidden_from_read_only_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``required_role=OPERATOR`` hides both tools from read-only operators.

    The list-time filter in
    :func:`~meho_backplane.mcp.registry.all_tools_for` strips entries
    above the operator's role rank. The dispatcher's call-time re-check
    is exercised separately in :mod:`tests.test_mcp_registry`.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tool_names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "search_knowledge" not in tool_names
    assert "add_to_knowledge" not in tool_names


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_search_knowledge_binds_operator_tenant_to_retrieve(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The handler forwards the operator's tenant_id verbatim, never a caller-supplied one.

    A separate ``tenant_id`` field is intentionally NOT exposed on the
    tool's input schema — the agent never gets to pick which tenant to
    search. The handler always binds :attr:`Operator.tenant_id`,
    which the JWT validator resolved upstream. Mocking
    ``retrieve`` and asserting the captured ``tenant_id`` kwarg pins
    the boundary at the handler level; the substrate's SQL filter
    enforces it at the storage level. PG-real cross-tenant coverage
    is in :mod:`tests.integration.test_kb_service_pg`.
    """
    client, op = client_with_operator
    captured: dict[str, object] = {}

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return []

    with patch("meho_backplane.kb.service.retrieve", side_effect=fake_retrieve):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_knowledge",
                    "arguments": {"query": "anything"},
                },
            },
        )
    assert response.status_code == 200
    # Operator's tenant_id, never a caller-supplied one — the input
    # schema has no tenant_id field by design.
    assert captured["tenant_id"] == op.tenant_id
