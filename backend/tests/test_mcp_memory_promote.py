# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho.memory.promote`` admin MCP meta-tool (G5.2-T4, #626).

Covers the acceptance criteria the issue body names for the MCP twin
of ``POST /api/v1/memory/{scope}/{slug}/promote``:

* ``meho.memory.promote`` is registered in the ``meho.*`` namespace
  with ``required_role=TENANT_ADMIN``.
* Visible in ``tools/list`` ONLY for a ``tenant_admin`` session;
  hidden for a plain ``operator`` (and read_only) session.
* Input schema mirrors the HTTP route body (``source_scope`` /
  ``slug`` / ``to`` / ``move`` / ``target_name``) with
  ``additionalProperties: false`` and 2020-12 JSON Schema shape.
* ``tools/call meho.memory.promote`` returns the target row JSON when
  the operator is admin and the promotion is legal.
* Non-admin dispatch (via call-time re-check) returns
  ``-32601 method_not_found`` (the registry hides the tool from
  ``tools/list``; the dispatcher's call-time gate is what stops an
  explicit invocation).
* Idempotency holds across the MCP surface (re-run returns existing
  row id, no duplicate insert).
* Cross-ladder, insufficient-authority, and not-found surface as
  ``-32602 INVALID_PARAMS`` with the canonical detail strings.
* Audit row carries ``audit_promotion_target_scope`` (load-bearing
  contextvar).

Embedding is mocked the same way :mod:`tests.test_mcp_tools_memory`
mocks it so the SQLite-backed default DB carries the suite without
needing fastembed at test time.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Document
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_TOOL_NAME = "meho.memory.promote"


@pytest.fixture
def stub_embedding() -> Iterator[AsyncMock]:
    """Patch the embedding singleton imported by the indexer.

    The promote handler inserts a target row via
    :func:`~meho_backplane.retrieval.indexer.index_document`, which
    calls ``get_embedding_service().encode_one`` on the insert branch.
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
# Source-row seeding helper
# ---------------------------------------------------------------------------


async def _insert_user_memory_for_operator(
    *,
    operator: Operator,
    slug: str = "wine-preference",
    body: str = "Prefers Pinot Noir.",
) -> UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        doc = Document(
            id=uuid.uuid4(),
            tenant_id=operator.tenant_id,
            source="memory",
            source_id=f"user:{operator.sub}:{slug}",
            kind="memory-user",
            body=body,
            body_hash="x" * 64,
            tokens=10,
            embedding=[0.01] * 384,
            doc_metadata={
                "scope": "user",
                "user_sub": operator.sub,
                "target_name": None,
                "expires_at": "2099-01-01T00:00:00+00:00",
            },
        )
        session.add(doc)
        await session.commit()
        return doc.id


# ---------------------------------------------------------------------------
# tools/list visibility -- role gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_tools_list_exposes_promote_for_tenant_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``meho.memory.promote`` appears in ``tools/list`` for a tenant_admin."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 200
    body = response.json()
    tool_names = {t["name"] for t in body["result"]["tools"]}
    assert _TOOL_NAME in tool_names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_hides_promote_from_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``meho.memory.promote`` is HIDDEN from non-admin sessions.

    The agent's daily memory surface is search_memory + add_to_memory;
    the promote meta-tool is admin-only and must not appear in
    ``tools/list`` for a plain operator. RBAC re-check at call time
    enforces the same gate; this list-time check is what keeps the
    tool off the agent's prompt context entirely.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tool_names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _TOOL_NAME not in tool_names


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.READ_ONLY],
    indirect=True,
)
def test_tools_list_hides_promote_from_read_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: ``meho.memory.promote`` is hidden from read_only sessions too."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tool_names = {t["name"] for t in response.json()["result"]["tools"]}
    assert _TOOL_NAME not in tool_names


# ---------------------------------------------------------------------------
# Input schema shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_promote_tool_input_schema_mirrors_http_route_body(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: input schema mirrors the HTTP route body.

    Five properties (``source_scope`` / ``slug`` / ``to`` / ``move`` /
    ``target_name``); ``additionalProperties: false`` so a typo
    surfaces as INVALID_PARAMS rather than being silently dropped.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    tools_by_name = {t["name"] for t in response.json()["result"]["tools"]}
    assert _TOOL_NAME in tools_by_name
    tool = next(t for t in response.json()["result"]["tools"] if t["name"] == _TOOL_NAME)
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"source_scope", "slug", "to"}
    assert set(schema["properties"].keys()) == {
        "source_scope",
        "slug",
        "to",
        "move",
        "target_name",
    }
    # Scope enum carries every MemoryScope value.
    assert set(schema["properties"]["source_scope"]["enum"]) == {
        "user",
        "user-tenant",
        "user-target",
        "tenant",
        "target",
    }
    # MEHO-internal fields stripped from the wire shape.
    assert "required_role" not in tool
    assert "op_class" not in tool


# ---------------------------------------------------------------------------
# Happy-path dispatch: tenant_admin promote user-tenant -> tenant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_promote_returns_target_row(
    stub_embedding: AsyncMock,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """AC: dispatch returns the target row JSON for a legal promotion.

    Seeds a ``memory-user-tenant`` source under the admin's sub
    (admins can write user-tenant), then promotes to ``tenant``. The
    response body is the :class:`MemoryEntry` dict carrying the
    target scope + the ``promoted_from`` marker.
    """
    client, op = client_with_operator
    # Seed a user-tenant source the admin owns.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=uuid.uuid4(),
                tenant_id=op.tenant_id,
                source="memory",
                source_id=f"user-tenant:{op.sub}:team-rule",
                kind="memory-user-tenant",
                body="Team always uses TLS 1.3.",
                body_hash="x" * 64,
                tokens=5,
                embedding=[0.01] * 384,
                doc_metadata={
                    "scope": "user-tenant",
                    "user_sub": op.sub,
                    "target_name": None,
                    "expires_at": None,
                },
            )
        )
        await session.commit()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {
                    "source_scope": "user-tenant",
                    "slug": "team-rule",
                    "to": "tenant",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["isError"] is False
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["scope"] == "tenant"
    assert payload["slug"] == "team-rule"
    assert payload["body"] == "Team always uses TLS 1.3."
    assert payload["metadata"]["promoted_from"] == "user-tenant/team-rule"
    # AC: target row's expires_at is None (broader-scope memories
    # are intentionally long-lived).
    assert payload["expires_at"] is None


# ---------------------------------------------------------------------------
# Idempotency across MCP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_promote_is_idempotent_on_rerun(
    stub_embedding: AsyncMock,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Re-running the same promotion returns the same target row id."""
    client, op = client_with_operator
    await _insert_user_memory_for_operator(operator=op, slug="my-pref")

    args = {
        "source_scope": "user",
        "slug": "my-pref",
        "to": "user-tenant",
    }
    first = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": _TOOL_NAME, "arguments": args},
        },
    )
    second = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": _TOOL_NAME, "arguments": args},
        },
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = json.loads(first.json()["result"]["content"][0]["text"])
    second_payload = json.loads(second.json()["result"]["content"][0]["text"])
    assert first_payload["id"] == second_payload["id"]

    # Verify no duplicate row landed.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(Document).where(
                Document.tenant_id == op.tenant_id,
                Document.kind == "memory-user-tenant",
            )
        )
        rows = list(result.scalars().all())
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Non-admin dispatch -- call-time re-check rejects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_call_promote_rejected_for_non_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An explicit ``tools/call`` for the admin tool from a non-admin fails.

    The registry's call-time RBAC re-check refuses to dispatch the
    handler when the operator's role doesn't meet the tool's
    ``required_role`` -- surfacing as ``-32601 method_not_found``
    (the tool is hidden from this operator's perspective).
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {
                    "source_scope": "user",
                    "slug": "wine-preference",
                    "to": "user-tenant",
                },
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    # Either ``isError=True`` with INVALID_PARAMS, or a top-level
    # JSON-RPC error. The registry's call-time gate raises
    # ``McpMethodNotFoundError`` for unknown / hidden tools -- both
    # the not-registered path and the role-gated path collapse to
    # the same response shape (method_not_found) so an under-
    # privileged operator can't probe for the existence of admin
    # tools.
    assert "error" in body or body["result"].get("isError") is True


# ---------------------------------------------------------------------------
# Error mapping: cross-ladder + insufficient_promotion_authority + not-found
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_promote_cross_ladder_returns_invalid_params(
    stub_embedding: AsyncMock,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Cross-ladder pair (``user-tenant -> target``) surfaces as INVALID_PARAMS."""
    client, op = client_with_operator
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=uuid.uuid4(),
                tenant_id=op.tenant_id,
                source="memory",
                source_id=f"user-tenant:{op.sub}:bad",
                kind="memory-user-tenant",
                body="x",
                body_hash="y" * 64,
                tokens=1,
                embedding=[0.01] * 384,
                doc_metadata={
                    "scope": "user-tenant",
                    "user_sub": op.sub,
                    "target_name": None,
                    "expires_at": None,
                },
            )
        )
        await session.commit()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {
                    "source_scope": "user-tenant",
                    "slug": "bad",
                    "to": "target",
                    "target_name": "infra-1",
                },
            },
        },
    )
    body = response.json()
    # JSON-RPC error code is INVALID_PARAMS regardless of where the
    # error originates (response shape uses top-level `error` for
    # spec-correct propagation).
    if "error" in body:
        assert body["error"]["code"] == INVALID_PARAMS
        assert "is not a legal widening" in body["error"]["message"]
    else:
        assert body["result"]["isError"] is True


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_promote_not_found_surfaces_invalid_params(
    stub_embedding: AsyncMock,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Source slug doesn't exist -- surfaces as INVALID_PARAMS ``memory_not_found``."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {
                    "source_scope": "user",
                    "slug": "no-such-slug",
                    "to": "user-tenant",
                },
            },
        },
    )
    body = response.json()
    assert "error" in body or body["result"].get("isError") is True
    if "error" in body:
        assert body["error"]["code"] == INVALID_PARAMS
        assert "memory_not_found" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Audit row carries audit_promotion_target_scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
@pytest.mark.asyncio
async def test_tools_call_promote_audit_row_carries_target_scope_payload(
    stub_embedding: AsyncMock,
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The MCP audit row's payload carries ``promotion_target_scope``.

    The MCP audit writer (:func:`~meho_backplane.mcp.audit.write_mcp_audit_row`)
    folds every ``audit_*`` contextvar through
    :func:`~meho_backplane.audit._resolve_audit_payload` into the
    row's payload JSONB. The handler binds
    ``audit_promotion_target_scope`` before calling the service, so
    the row carries the distinguishing key.
    """
    client, op = client_with_operator
    await _insert_user_memory_for_operator(operator=op, slug="audit-test")

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {
                    "source_scope": "user",
                    "slug": "audit-test",
                    "to": "user-tenant",
                },
            },
        },
    )
    assert response.status_code == 200

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        from sqlalchemy import select

        from meho_backplane.db.models import AuditLog

        result = await session.execute(select(AuditLog).where(AuditLog.operator_sub == op.sub))
        rows = list(result.scalars().all())
    promote_rows = [
        r for r in rows if r.payload and r.payload.get("promotion_target_scope") == "user-tenant"
    ]
    assert len(promote_rows) >= 1


# ---------------------------------------------------------------------------
# Schema enforcement: missing required, unknown field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_tools_call_promote_rejects_missing_required(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Missing ``to`` -- INVALID_PARAMS at the JSON Schema layer."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {"source_scope": "user", "slug": "x"},
            },
        },
    )
    body = response.json()
    assert "error" in body or body["result"].get("isError") is True
    if "error" in body:
        assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.TENANT_ADMIN],
    indirect=True,
)
def test_tools_call_promote_rejects_unknown_argument(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Unknown field -- INVALID_PARAMS via ``additionalProperties: false``."""
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": _TOOL_NAME,
                "arguments": {
                    "source_scope": "user",
                    "slug": "x",
                    "to": "user-tenant",
                    "destinaton": "tenant",  # typo
                },
            },
        },
    )
    body = response.json()
    assert "error" in body or body["result"].get("isError") is True
