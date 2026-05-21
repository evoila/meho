# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``search_memory`` / ``add_to_memory`` MCP tools (G5.1-T3, #423).

Covers every acceptance criterion in the task body that targets the
tool surface:

* Both meta-tools are registered against the G0.5 registry; ``tools/list``
  surfaces them with strict 2020-12 ``inputSchema``
  (``additionalProperties: false``) and the MEHO-internal RBAC fields
  stripped from the wire shape.
* ``inputSchema`` ``required`` lists match the spec: ``[query]`` for
  ``search_memory``; ``[body, scope]`` for ``add_to_memory`` (renamed
  from ``content`` to align with ``add_to_knowledge`` + the REST
  ``POST /api/v1/memory`` body schema per G0.9.1-T7 (#779)).
* Tool descriptions name what + when + which scope + cross-reference
  G5.2's TTL default (load-bearing per the AI-engineering anchor).
* ``tools/call search_memory`` against a seeded corpus returns ranked
  hits adapted from :class:`MemoryEntrySearchHit`.
* ``tools/call add_to_memory`` creates the entry; a follow-up
  ``search_memory`` finds it.
* RBAC: ``operator`` writing ``TENANT`` is denied (INVALID_PARAMS),
  ``tenant_admin`` writing ``TENANT`` succeeds.
* Tenant boundary: a search initiated under tenant A never returns
  tenant B's entries.
* TTL parsing: ``P7D`` / ``PT1H`` accepted; ``P1Y`` rejected;
  malformed strings rejected.
* Audit + broadcast emit per call via the shared dispatcher path
  (covered transitively in :mod:`tests.test_mcp_audit`).

Embedding is mocked the same way :mod:`tests.test_memory_service` mocks
it, so the SQLite-backed default DB carries the suite without needing
fastembed at test time. The retrieve substrate's PG-only BM25 + cosine
SQL is patched via :func:`patch` on
:func:`meho_backplane.retrieval.retriever.retrieve`; the wire-shape
contract here is the **MCP surface** plus end-to-end service round-trip
on SQLite, with PG-real ranking coverage living in the integration suite.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.memory.schemas import MemoryScope
from meho_backplane.memory.service import MemoryService
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

    Mirrors the embedding stub in :mod:`tests.test_memory_service` — the
    indexer's embedding compute is the only path that pulls fastembed /
    ONNX runtime, so patching at the import site lets the SQLite-backed
    test DB carry every assertion below.
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
def test_tools_list_exposes_memory_tools_with_strict_input_schema(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: both tools appear with strict 2020-12 schemas.

    ``search_memory`` requires ``[query]``; ``add_to_memory`` requires
    ``[body, scope]``. Both have ``additionalProperties: false``.
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

    assert "search_memory" in tools_by_name
    search = tools_by_name["search_memory"]
    assert search["inputSchema"]["type"] == "object"
    assert search["inputSchema"]["required"] == ["query"]
    assert search["inputSchema"]["additionalProperties"] is False
    assert "query" in search["inputSchema"]["properties"]
    assert "scope" in search["inputSchema"]["properties"]
    assert "limit" in search["inputSchema"]["properties"]
    assert "required_role" not in search
    assert "op_class" not in search

    assert "add_to_memory" in tools_by_name
    add = tools_by_name["add_to_memory"]
    assert add["inputSchema"]["required"] == ["body", "scope"]
    assert add["inputSchema"]["additionalProperties"] is False
    assert "body" in add["inputSchema"]["properties"]
    # G0.9.1-T7 (#779): the old ``content`` name is gone; ``extra="forbid"``
    # via additionalProperties:false means a v0.3.1 client posting
    # ``content`` will fail loud at the JSON-Schema gate.
    assert "content" not in add["inputSchema"]["properties"]
    assert "scope" in add["inputSchema"]["properties"]
    assert "ttl" in add["inputSchema"]["properties"]
    assert "target_name" in add["inputSchema"]["properties"]
    assert "slug" in add["inputSchema"]["properties"]
    assert "tags" in add["inputSchema"]["properties"]
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
    """AC: descriptions name what + when + which scope + G5.2 TTL cross-reference.

    Smoke-asserts the load-bearing pieces of the description prose so a
    future edit that removes them surfaces here rather than as silently
    degraded agent UX. Keeps assertions loose (substring presence) so
    prose can evolve without churn.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools_by_name = {t["name"]: t for t in response.json()["result"]["tools"]}

    search_desc = tools_by_name["search_memory"]["description"]
    # What: memory recall.
    assert "memor" in search_desc.lower()
    # When to use: recall established conventions / preferences.
    assert "recall" in search_desc.lower() or "established" in search_desc.lower()
    # When NOT to use: durable team knowledge → kb.
    assert "knowledge base" in search_desc.lower() or "search_knowledge" in search_desc
    # Cross-reference to the companion resource for the full body.
    assert "meho://memory/" in search_desc
    # Scope enumeration discipline (names every scope so an agent
    # without a tool listing can still pick correctly).
    for scope_value in (s.value for s in MemoryScope):
        assert scope_value in search_desc

    add_desc = tools_by_name["add_to_memory"]["description"]
    # When to use: capturing retainable session learning.
    assert "retain" in add_desc.lower() or "preference" in add_desc.lower()
    # Cross-reference G5.2's TTL default (load-bearing per the issue body).
    assert "G5.2" in add_desc or "7-day" in add_desc
    # Each scope is named explicitly.
    for scope_value in (s.value for s in MemoryScope):
        assert scope_value in add_desc
    # Search-first discipline (avoid corpus fragmentation).
    assert "search_memory" in add_desc
    # When NOT to use: kb is the durable surface.
    assert "add_to_knowledge" in add_desc


# ---------------------------------------------------------------------------
# search_memory — call path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_search_memory_returns_ranked_hits(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: search returns ranked hits adapted to :class:`MemoryEntrySearchHit`.

    Mocks the retrieve substrate the same way
    :mod:`tests.test_memory_service` does for its search test — the
    PG-only ``@@`` / ``<=>`` ranking is exercised in the integration
    suite. Here the assertion is the MCP wire-shape contract: hits
    arrive with the (scope, slug) pair, the body, and the fused score
    round-tripping through ``model_dump(mode="json")`` and the
    dispatcher's ``content[0].text`` JSON envelope.
    """
    client, op = client_with_operator
    captured: dict[str, object] = {}

    ts = datetime(2026, 5, 21, 10, 16, 12, tzinfo=UTC)
    fake_hit = RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=op.tenant_id,
        source="memory",
        source_id=f"user:{op.sub}:wine-preference",
        kind="memory-user",
        body="Prefers a 2019 Brunello with steak.",
        doc_metadata={
            "user_sub": op.sub,
            "target_name": None,
            "expires_at": None,
            "scope": "user",
        },
        created_at=ts,
        updated_at=ts,
        fused_score=0.7,
        bm25_score=0.4,
        cosine_score=0.85,
        bm25_rank=1,
        cosine_rank=1,
    )

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return [fake_hit]

    with patch(
        "meho_backplane.memory.service.retrieve",
        side_effect=fake_retrieve,
    ):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_memory",
                    "arguments": {"query": "wine"},
                },
            },
        )

    assert captured["source"] == "memory"
    assert captured["tenant_id"] == op.tenant_id
    assert captured["query"] == "wine"
    # No scope filter → kind passed as None to retrieve (service-side
    # post-filter on `visible_kinds` is the matrix gate).
    assert captured["kind"] is None

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert "hits" in payload
    assert isinstance(payload["hits"], list)
    assert len(payload["hits"]) == 1
    hit = payload["hits"][0]
    # The hit wraps a MemoryEntry under `entry` plus the ranking metadata.
    assert hit["entry"]["scope"] == "user"
    assert hit["entry"]["slug"] == "wine-preference"
    assert hit["entry"]["body"] == "Prefers a 2019 Brunello with steak."
    assert hit["fused_score"] == pytest.approx(0.7)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_search_memory_forwards_scope_and_limit(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The ``scope`` and ``limit`` arguments reach the retrieve call.

    The handler translates the wire string ``scope`` into the typed
    enum and forwards ``limit`` verbatim; the fake retrieve captures
    both so we can assert the plumbing without exercising the SQL.
    """
    client, _op = client_with_operator
    captured: dict[str, object] = {}

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return []

    with patch(
        "meho_backplane.memory.service.retrieve",
        side_effect=fake_retrieve,
    ):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_memory",
                    "arguments": {
                        "query": "x",
                        "scope": "tenant",
                        "limit": 5,
                    },
                },
            },
        )
    assert response.status_code == 200
    # `kind` reflects the scope-to-kind translation
    # (`memory-tenant`) the service performs before reaching retrieve.
    assert captured["kind"] == "memory-tenant"
    # The service pulls `limit * 4` candidates for the RBAC post-filter
    # pass; assert at least the limit was forwarded.
    assert captured["limit"] == max(5 * 4, 50)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_search_memory_rejects_missing_query(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: missing required ``query`` fails JSON-Schema validation → -32602."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search_memory", "arguments": {}},
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
def test_tools_call_search_memory_rejects_extra_arguments(
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
                "name": "search_memory",
                "arguments": {"query": "anything", "unknown_field": 42},
            },
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
def test_tools_call_search_memory_rejects_unrecognised_scope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The ``scope`` enum constraint blocks values outside the five scopes."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_memory",
                "arguments": {"query": "x", "scope": "nonexistent-scope"},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# add_to_memory — call path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_memory_creates_entry_and_is_recallable(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: add_to_memory creates the row; the entry is recallable via the service.

    Writes through the MCP transport, then verifies the row landed by
    reading back through :meth:`MemoryService.recall` (SELECT-only —
    portable across SQLite + PG, so this test exercises the full
    write path without depending on the substrate's PG-only retrieval
    SQL). The PG-real round-trip through ``search_memories`` is in
    the integration suite; the wire-shape contract here is the
    response shape from ``add_to_memory`` (full :class:`MemoryEntry`
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
                "name": "add_to_memory",
                "arguments": {
                    "body": "Operator prefers concise CLI output.",
                    "scope": "user",
                    "slug": "cli-output-preference",
                    "tags": ["preference"],
                },
            },
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["result"]["isError"] is False
    created = json.loads(body["result"]["content"][0]["text"])
    assert created["scope"] == "user"
    assert created["slug"] == "cli-output-preference"
    assert created["body"] == "Operator prefers concise CLI output."
    assert created["metadata"]["tags"] == ["preference"]
    # MemoryEntry model_dump payload carries the substrate-side id +
    # timestamps; assert their shape so a future field-rename surfaces here.
    assert "id" in created
    assert "created_at" in created
    assert "updated_at" in created

    # The substrate row is actually present and recallable by the operator.
    service = MemoryService()
    fetched = await service.recall(op, scope=MemoryScope.USER, slug="cli-output-preference")
    assert fetched is not None
    assert fetched.body == "Operator prefers concise CLI output."
    assert fetched.metadata["tags"] == ["preference"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_memory_applies_ttl(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: ``ttl`` parses an ISO 8601 duration into an ``expires_at`` window.

    A ``P7D`` ttl yields an ``expires_at`` ~7 days from now (allowing
    a few seconds of jitter for the round-trip). The returned
    :class:`MemoryEntry` carries the parsed timestamp under
    :attr:`expires_at`.
    """
    client, _op = client_with_operator

    before = datetime.now(UTC)
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Short-lived note.",
                    "scope": "user",
                    "ttl": "P7D",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["expires_at"] is not None
    parsed = datetime.fromisoformat(payload["expires_at"])
    # 7 days ± a wide window so a slow test machine never flakes.
    expected = before + timedelta(days=7)
    delta = abs((parsed - expected).total_seconds())
    assert delta < 60, f"expected ~7 days from {before}, got {parsed} (delta={delta}s)"


# ---------------------------------------------------------------------------
# G0.9.1-T3 (#775): default-TTL injection on the MCP add_to_memory path
# ---------------------------------------------------------------------------
#
# The MCP handler delegates the default-TTL decision to the same
# :func:`meho_backplane.memory.ttl.resolve_default_expires_at` resolver
# the REST handler uses. The discrimination on the MCP side is
# ``"ttl" in arguments`` (the dispatcher only carries keys the inbound
# JSON-RPC payload supplied, gated by ``additionalProperties: false``);
# the canonical opt-outs match the REST surface:
#
# * ``ttl`` omitted on user-scope → ``now + 7d`` default (load-bearing
#   parity with REST's ``model_fields_set`` branch on the same shape).
# * Explicit ``"ttl": null`` on user-scope → no expiry (CLI ``--persist``
#   parity).
# * Non-user scopes (``user-tenant`` etc.) → no default even with ``ttl``
#   omitted, mirroring #624's narrow gate.


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_memory_user_scope_no_ttl_injects_default(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC #1: omitted ``ttl`` on ``user`` scope → ``expires_at ~ now + 7d``.

    Closes the regression #775 documents: prior to G0.9.1 the MCP path
    always passed ``expires_at`` explicitly to ``service.remember``
    (including ``None`` when ``ttl`` was absent), defeating the
    surface-layer "set vs unset" split and silently producing memories
    with ``expires_at=null`` -- so user-scope MCP writes lived forever
    despite the documented 7-day default.

    Tolerance window mirrors the existing ``applies_ttl`` test:
    ``parsed`` should land within ±60 s of ``before + 7 d`` so a slow
    test machine never flakes.
    """
    client, _op = client_with_operator

    before = datetime.now(UTC)
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Operator-scope note that should age out.",
                    "scope": "user",
                    "slug": "default-ttl-user",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    # The persisted row carries the injected default. Without the fix
    # this would be ``None``.
    assert payload["expires_at"] is not None, (
        "user-scope MCP write with omitted ttl must inject the default "
        f"expires_at; got payload={payload}"
    )
    parsed = datetime.fromisoformat(payload["expires_at"])
    expected = before + timedelta(days=7)
    delta = abs((parsed - expected).total_seconds())
    assert delta < 60, f"expected ~7 days from {before}, got {parsed} (delta={delta}s)"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_memory_user_scope_explicit_null_ttl_persists(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC #2: explicit ``"ttl": null`` on ``user`` scope → no expiry.

    Load-bearing opt-out parity with the REST surface: the CLI
    ``--persist`` flag emits ``"expires_at": null`` and the agent
    surface mirrors it with ``"ttl": null``. Discrimination is
    ``"ttl" in arguments`` -- ``arguments.get("ttl") is None`` would
    collapse this branch into the default path and silently override
    the operator's pin-forever intent.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Pin this note forever (CLI --persist parity).",
                    "scope": "user",
                    "slug": "persist-forever-user",
                    "ttl": None,
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["expires_at"] is None, (
        f"explicit ttl=null must opt out of the default; got payload={payload}"
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_memory_user_tenant_scope_no_default_ttl(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC #3: non-``user`` scopes bypass the default even with ``ttl`` omitted.

    #624 narrows the default to ``kind == "memory-user"``. Pinning the
    ``user-tenant`` scope here prevents a future refactor that widened
    the gate to ``USER_SCOPED`` from silently expiring team-shared
    memories on the MCP path. Parity with the equivalent REST test
    ``test_remember_user_tenant_scope_no_default_ttl``.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Team-shared note in this tenant.",
                    "scope": "user-tenant",
                    "slug": "team-note-mcp",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["expires_at"] is None, (
        f"non-user scope must not get the default ttl; got payload={payload}"
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_add_to_memory_rejects_year_ttl(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Year/month/week TTL components are rejected (variable-length).

    The handler accepts ``P[nD][T...]`` shapes only; ``P1Y`` surfaces as
    INVALID_PARAMS with the unsupported-unit message.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "x",
                    "scope": "user",
                    "ttl": "P1Y",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "unsupported unit" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_add_to_memory_rejects_malformed_ttl(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A ttl that isn't an ISO 8601 duration → INVALID_PARAMS."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "x",
                    "scope": "user",
                    "ttl": "seven days",
                },
            },
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
def test_tools_call_add_to_memory_operator_denied_tenant_scope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: an ``operator`` role attempting a ``TENANT`` write → INVALID_PARAMS.

    The service-side
    :class:`~meho_backplane.memory.rbac.MemoryRbacResolver` denies the
    write; the handler maps :class:`PermissionDeniedError` to
    :class:`McpInvalidParamsError`. JSON-RPC has no HTTP-403 analogue;
    INVALID_PARAMS is the spec-correct lane for caller-input failures
    of this shape.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Tenant-wide convention.",
                    "scope": "tenant",
                    "slug": "tenant-convention",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "permission" in body["error"]["message"].lower() or (
        "scope=tenant" in body["error"]["message"].lower()
    )


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_add_to_memory_tenant_admin_can_write_tenant_scope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """AC: ``tenant_admin`` writing ``TENANT`` succeeds; entry is created."""
    client, op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Use Brunello for all wine-pairing demos.",
                    "scope": "tenant",
                    "slug": "wine-pairing-tenant-default",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["scope"] == "tenant"
    assert payload["slug"] == "wine-pairing-tenant-default"

    # The substrate row is present and recallable by the same admin
    # (tenant-scope reads are open to every operator in the tenant).
    service = MemoryService()
    fetched = await service.recall(
        op,
        scope=MemoryScope.TENANT,
        slug="wine-pairing-tenant-default",
    )
    assert fetched is not None
    assert fetched.body == "Use Brunello for all wine-pairing demos."


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_add_to_memory_requires_target_name_for_target_scope(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    stub_embedding: AsyncMock,
) -> None:
    """A ``target``-scope write without ``target_name`` → INVALID_PARAMS.

    The service raises :class:`ValueError` before reaching the indexer
    when the target-scope contract is violated; the handler maps that
    to INVALID_PARAMS.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "body": "Target-specific gotcha.",
                    "scope": "target",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "target_name" in body["error"]["message"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_add_to_memory_rejects_missing_required(
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
                "name": "add_to_memory",
                "arguments": {"scope": "user"},
            },
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
def test_tools_call_add_to_memory_rejects_legacy_content_field(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """G0.9.1-T7 (#779): the old ``content`` field is now rejected.

    The MCP `add_to_memory` tool renamed `content` -> `body` to align
    with `add_to_knowledge` and the REST `POST /api/v1/memory` body
    schema. ``additionalProperties: false`` on the inputSchema means a
    v0.3.1 client posting ``content`` (without ``body``) fails the
    JSON-Schema gate with INVALID_PARAMS -- not a silent drop. This is
    the breaking-change contract documented in CHANGELOG [0.3.2].
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "add_to_memory",
                "arguments": {
                    "content": "legacy v0.3.1 wire shape",
                    "scope": "user",
                },
            },
        },
    )
    assert response.status_code == 200
    err = response.json()["error"]
    assert err["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# RBAC visibility — read_only operator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_memory_tools_hidden_from_read_only_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``required_role=OPERATOR`` hides both tools from read-only operators."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tool_names = {t["name"] for t in response.json()["result"]["tools"]}
    assert "search_memory" not in tool_names
    assert "add_to_memory" not in tool_names


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_search_memory_binds_operator_tenant_to_retrieve(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The handler forwards the operator's tenant_id verbatim, never a caller-supplied one.

    A separate ``tenant_id`` field is intentionally NOT exposed on the
    tool's input schema — the agent never gets to pick which tenant to
    search. The handler always binds :attr:`Operator.tenant_id`,
    which the JWT validator resolved upstream. Mocking ``retrieve``
    and asserting the captured ``tenant_id`` kwarg pins the boundary
    at the handler level.
    """
    client, op = client_with_operator
    captured: dict[str, Any] = {}

    async def fake_retrieve(**kwargs: object) -> list[RetrievalHit]:
        captured.update(kwargs)
        return []

    with patch(
        "meho_backplane.memory.service.retrieve",
        side_effect=fake_retrieve,
    ):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_memory",
                    "arguments": {"query": "anything"},
                },
            },
        )
    assert response.status_code == 200
    assert captured["tenant_id"] == op.tenant_id
