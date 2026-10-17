# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``POST /api/v1/memory/search`` (#3741).

The route is a thin adapter over :meth:`MemoryService.search_memories`.
These tests pin:

* the route builds the same typed Memory eligibility as the MCP
  ``search_memory`` tool, so CLI recall and an agent see the same hits;
* the all-scopes default and ``scope`` narrowing;
* ``read_only`` may search (like the other memory reads);
* an empty result is exactly ``{"hits": []}`` -- no count, no total --
  so "no match" and "only rows you may not see" look the same;
* request limits (query 1..256, limit 1..50, unknown fields rejected);
* the audit row says ``memory.search`` / ``read`` and never the query.

The eligibility SQL itself is PostgreSQL-only and is proven in
``tests/integration/test_memory_eligibility_pg.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.memory import router as memory_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.tools.memory import _search_memory_handler
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.retrieval.retriever import RetrievalHit
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_SEARCH_PATH = "/api/v1/memory/search"
_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c0c0")
_ALL_KINDS = (
    "memory-user",
    "memory-user-tenant",
    "memory-user-target",
    "memory-tenant",
    "memory-target",
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    clear_jwks_cache()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    """App with the production middleware stack and only the memory router."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(memory_router)
    return TestClient(app)


def _search(
    client: TestClient,
    payload: dict[str, Any],
    *,
    sub: str = "op-alice",
    role: TenantRole = TenantRole.OPERATOR,
) -> Any:
    """POST *payload* to the search route as *sub* with *role*."""
    key = _make_rsa_keypair("kid-search")
    token = _mint_token(key, sub=sub, tenant_role=role.value, tenant_id=str(_TENANT))
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        return client.post(_SEARCH_PATH, json=payload, headers={"Authorization": f"Bearer {token}"})


def _hit(*, kind: str, slug: str, user_sub: str | None, target_name: str | None) -> RetrievalHit:
    """A substrate hit shaped like a stored memory row."""
    ts = datetime(2026, 9, 1, 8, 0, 0, tzinfo=UTC)
    return RetrievalHit(
        document_id=uuid.uuid4(),
        tenant_id=_TENANT,
        source="memory",
        source_id=f"stub:{slug}",
        kind=kind,
        body=f"body of {slug}",
        doc_metadata={
            "scope": kind.removeprefix("memory-"),
            "user_sub": user_sub,
            "target_name": target_name,
            "expires_at": None,
        },
        created_at=ts,
        updated_at=ts,
        fused_score=0.03,
        bm25_score=0.2,
        cosine_score=0.8,
        bm25_rank=1,
        cosine_rank=2,
    )


_HITS = [
    _hit(kind="memory-user", slug="own-note", user_sub="op-alice", target_name=None),
    _hit(kind="memory-target", slug="shared-target", user_sub=None, target_name="infra-1"),
]


def test_search_defaults_to_all_visible_scopes(client: TestClient) -> None:
    """No ``scope`` -> one retrieve with all five kinds for the caller."""
    fake = AsyncMock(return_value=_HITS)
    with patch("meho_backplane.memory.service.retrieve", new=fake):
        response = _search(client, {"query": "rotation runbook"})

    assert response.status_code == 200
    body = response.json()
    assert [h["entry"]["slug"] for h in body["hits"]] == ["own-note", "shared-target"]
    assert body["hits"][1]["entry"]["target_name"] == "infra-1"
    kwargs = fake.await_args.kwargs
    assert kwargs["source"] == "memory"
    assert kwargs["limit"] == 10
    eligibility = kwargs["memory_eligibility"]
    assert eligibility.tenant_id == _TENANT
    assert eligibility.principal_sub == "op-alice"
    assert eligibility.kinds == _ALL_KINDS


def test_search_scope_narrows_to_one_kind(client: TestClient) -> None:
    """``scope`` narrows the eligibility; ``limit`` reaches the service as-is."""
    fake = AsyncMock(return_value=[])
    with patch("meho_backplane.memory.service.retrieve", new=fake):
        response = _search(client, {"query": "q", "scope": "user-target", "limit": 3})
    assert response.status_code == 200
    kwargs = fake.await_args.kwargs
    assert kwargs["memory_eligibility"].kinds == ("memory-user-target",)
    assert kwargs["limit"] == 3


def test_read_only_operator_may_search(client: TestClient) -> None:
    """``read_only`` reads memories elsewhere, so it may search too."""
    with patch("meho_backplane.memory.service.retrieve", new=AsyncMock(return_value=[])):
        response = _search(client, {"query": "q"}, role=TenantRole.READ_ONLY)
    assert response.status_code == 200


def test_empty_result_has_no_count(client: TestClient) -> None:
    """Foreign-only and no-match both answer exactly ``{"hits": []}``."""
    with patch("meho_backplane.memory.service.retrieve", new=AsyncMock(return_value=[])):
        response = _search(client, {"query": "someone else's secret"})
    assert response.status_code == 200
    assert response.json() == {"hits": []}


def test_unauthenticated_search_is_401(client: TestClient) -> None:
    """No token -> 401, no retrieval."""
    fake = AsyncMock(return_value=[])
    with patch("meho_backplane.memory.service.retrieve", new=fake):
        response = client.post(_SEARCH_PATH, json={"query": "q"})
    assert response.status_code == 401
    fake.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "x" * 257},
        {"query": "q", "limit": 0},
        {"query": "q", "limit": 51},
        {"query": "q", "scope": "everyone"},
        {"query": "q", "source": "kb"},
    ],
)
def test_invalid_request_is_422(client: TestClient, payload: dict[str, Any]) -> None:
    """Bad input is rejected before the service runs."""
    fake = AsyncMock(return_value=[])
    with patch("meho_backplane.memory.service.retrieve", new=fake):
        response = _search(client, payload)
    assert response.status_code == 422
    fake.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_audit_row_is_read_without_query(client: TestClient) -> None:
    """The audit row names ``memory.search`` / ``read`` and never the query text."""
    with patch("meho_backplane.memory.service.retrieve", new=AsyncMock(return_value=[])):
        response = _search(client, {"query": "SECRET QUERY TEXT", "scope": "tenant"})
    assert response.status_code == 200

    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == _SEARCH_PATH)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["op_id"] == "memory.search"
    assert payload["op_class"] == "read"
    assert payload["scope"] == "tenant"
    assert "SECRET QUERY TEXT" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, "user", "target"])
async def test_rest_and_mcp_return_the_same_eligible_hits(
    client: TestClient, scope: str | None
) -> None:
    """CLI recall (REST) and MCP ``search_memory`` give the same result for the same input."""
    rest_fake = AsyncMock(return_value=_HITS)
    payload: dict[str, Any] = {"query": "rotation runbook", "limit": 5}
    if scope is not None:
        payload["scope"] = scope
    with patch("meho_backplane.memory.service.retrieve", new=rest_fake):
        rest = _search(client, payload)
    assert rest.status_code == 200

    operator = Operator(
        sub="op-alice",
        name=None,
        email=None,
        raw_jwt="not-a-real-jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )
    mcp_fake = AsyncMock(return_value=_HITS)
    with patch("meho_backplane.memory.service.retrieve", new=mcp_fake):
        mcp = await _search_memory_handler(operator, payload)

    assert rest.json() == json.loads(json.dumps(mcp))
    rest_kwargs = rest_fake.await_args.kwargs
    mcp_kwargs = mcp_fake.await_args.kwargs
    assert rest_kwargs["memory_eligibility"] == mcp_kwargs["memory_eligibility"]
    assert rest_kwargs["limit"] == mcp_kwargs["limit"] == 5
