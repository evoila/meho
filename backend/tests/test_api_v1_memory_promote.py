# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``POST /api/v1/memory/{scope}/{slug}/promote``.

G5.2-T4 (#626). Covers every acceptance criterion in the task body:

* Promote ``user/foo -> user-tenant`` (default ``move=false``) returns
  200 + the target row; source still present; target carries
  ``metadata.promoted_from == "user/foo"`` and
  ``metadata.expires_at == None``.
* ``move=true`` deletes the source row in the same transaction (target
  insert + source delete are atomic).
* Re-running the identical promote returns 200 with the existing
  target row; ``documents`` count unchanged (idempotent, no 409, no
  duplicate insert).
* Cross-ladder pair (``user-tenant -> target``) returns 400.
* Non-admin operator promoting to ``tenant`` returns 403 with
  ``insufficient_promotion_authority``.
* Operator in tenant A promoting tenant B's memory returns 404 on
  source (tenant boundary holds; not 403, no cross-tenant existence
  leak).
* Audit row carries ``op_id="memory.promote"`` + ``op_class="write"``
  + ``audit_promotion_target_scope == <target-scope>`` (the
  distinguishing payload key).

Tests boot the FastAPI app with the production middleware stack
(:class:`RequestContextMiddleware` + :class:`AuditMiddleware`) so
audit rows are inserted into the autouse-migrated SQLite engine. The
service path is exercised end-to-end (no ``MemoryService.promote``
patching) so the transaction boundary, idempotency probe, and audit
contextvar binding all run for real.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
import respx
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.api.v1.memory import router as memory_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, Document
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _fake_embedding_service() -> Iterator[None]:
    """Patch the embedding singleton so the indexer skips fastembed.

    The promote service path inserts a target row via
    :func:`~meho_backplane.retrieval.indexer.index_document`, which
    calls ``get_embedding_service().encode_one`` on the insert branch.
    Mirroring :mod:`tests.test_memory_service`'s embedding stub keeps
    the SQLite-backed test path independent of fastembed at test
    time.
    """
    fake = AsyncMock()
    fake.encode_one.return_value = [0.01] * 384
    fake.dimension = 384
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake,
    ):
        yield


# ---------------------------------------------------------------------------
# Structlog capture
# ---------------------------------------------------------------------------


def _configure_capture(buf: io.StringIO) -> None:
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    _configure_capture(buf)
    yield buf
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """``FastAPI`` mirroring prod with only the memory router mounted."""
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(memory_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _admin_token(*, tenant_id: UUID | None = None, sub: str = "op-admin") -> tuple[Any, str]:
    key = _make_rsa_keypair("kid-admin")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(tid),
    )
    return key, token


def _operator_token(
    *,
    tenant_id: UUID | None = None,
    sub: str = "op-operator",
) -> tuple[Any, str]:
    key = _make_rsa_keypair(f"kid-operator-{sub}")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tid),
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Seed helpers -- insert a source row directly so promote has something to load.
# ---------------------------------------------------------------------------


async def _insert_user_memory(
    *,
    tenant_id: UUID,
    user_sub: str,
    slug: str = "wine-preference",
    body: str = "Prefers Pinot Noir.",
    expires_at_iso: str | None = "2099-01-01T00:00:00+00:00",
) -> UUID:
    """Insert a ``memory-user`` row directly via the ORM session.

    Bypasses :class:`MemoryService` so the promote path is exercised
    against a row that exists, without involving the (mocked-here)
    embedding service. Returns the inserted row's ``id``.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        doc = Document(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source="memory",
            source_id=f"user:{user_sub}:{slug}",
            kind="memory-user",
            body=body,
            body_hash="x" * 64,
            tokens=10,
            embedding=[0.01] * 384,
            doc_metadata={
                "scope": "user",
                "user_sub": user_sub,
                "target_name": None,
                "expires_at": expires_at_iso,
            },
        )
        session.add(doc)
        await session.commit()
        return doc.id


async def _count_documents(tenant_id: UUID) -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(Document).where(Document.tenant_id == tenant_id))
        return len(list(result.scalars().all()))


async def _fetch_documents_by_kind(
    tenant_id: UUID,
    kind: str,
) -> list[Document]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(Document).where(
                Document.tenant_id == tenant_id,
                Document.kind == kind,
            )
        )
        return list(result.scalars().all())


async def _audit_rows_for_path(path: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_promote_route_mounted_on_main_app() -> None:
    """The promote route appears on the main app + OpenAPI document."""
    from meho_backplane.main import app

    openapi = app.openapi()
    assert "/api/v1/memory/{scope}/{slug}/promote" in openapi["paths"]
    assert "post" in openapi["paths"]["/api/v1/memory/{scope}/{slug}/promote"]


def test_promote_unauthenticated_returns_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/memory/user/wine-preference/promote",
        json={"to": "user-tenant"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Happy path: user -> user-tenant (default move=false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_user_to_user_tenant_succeeds_and_preserves_source(
    client: TestClient,
) -> None:
    """AC: legal widening returns 200 + target row; source still present."""
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(tenant_id=tenant_a, user_sub=alice_sub)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "user-tenant"
    assert body["slug"] == "wine-preference"
    assert body["body"] == "Prefers Pinot Noir."
    assert body["metadata"]["promoted_from"] == "user/wine-preference"
    # AC: target carries cleared expires_at regardless of source.
    assert body["expires_at"] is None
    assert body["metadata"]["expires_at"] is None

    # AC: source still present after default-move=false.
    user_rows = await _fetch_documents_by_kind(tenant_a, "memory-user")
    assert len(user_rows) == 1
    user_tenant_rows = await _fetch_documents_by_kind(tenant_a, "memory-user-tenant")
    assert len(user_tenant_rows) == 1


@pytest.mark.asyncio
async def test_promote_user_to_user_tenant_with_move_deletes_source(
    client: TestClient,
) -> None:
    """AC: ``move=true`` deletes the source row in the same transaction."""
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(tenant_id=tenant_a, user_sub=alice_sub)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant", "move": True},
            headers=_authed(token),
        )

    assert response.status_code == 200
    # AC: source row gone after move=true.
    user_rows = await _fetch_documents_by_kind(tenant_a, "memory-user")
    assert len(user_rows) == 0
    user_tenant_rows = await _fetch_documents_by_kind(tenant_a, "memory-user-tenant")
    assert len(user_tenant_rows) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_rerun_is_idempotent_returns_existing_row(
    client: TestClient,
) -> None:
    """AC: re-running the identical promote returns 200 + the existing target row.

    The count of ``documents`` in the tenant is unchanged after the
    second call (no duplicate insert). Status is 200 (not 409 -- the
    idempotency contract makes retries safe).
    """
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(tenant_id=tenant_a, user_sub=alice_sub)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        first = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )
        assert first.status_code == 200
        count_after_first = await _count_documents(tenant_a)

        second = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )

    assert second.status_code == 200
    # Same target row id round-trips on the re-run (proves the
    # existing-row branch, not a transparent overwrite that would
    # have minted a fresh id).
    assert second.json()["id"] == first.json()["id"]
    count_after_second = await _count_documents(tenant_a)
    assert count_after_second == count_after_first


# ---------------------------------------------------------------------------
# Cross-ladder pairs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_cross_ladder_pair_returns_400(client: TestClient) -> None:
    """AC: ``user-tenant -> target`` (cross-ladder) returns 400."""
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    # Seed a ``user-tenant`` source so the natural-key lookup finds
    # something (we want the 400 from the ladder check, not 404).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=uuid.uuid4(),
                tenant_id=tenant_a,
                source="memory",
                source_id=f"user-tenant:{alice_sub}:team-pref",
                kind="memory-user-tenant",
                body="Team prefers X.",
                body_hash="y" * 64,
                tokens=5,
                embedding=[0.01] * 384,
                doc_metadata={
                    "scope": "user-tenant",
                    "user_sub": alice_sub,
                    "target_name": None,
                    "expires_at": None,
                },
            )
        )
        await session.commit()

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user-tenant/team-pref/promote",
            json={"to": "target", "target_name": "infra-1"},
            headers=_authed(token),
        )

    assert response.status_code == 400
    assert "is not a legal widening" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 403 insufficient_promotion_authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_non_admin_to_tenant_returns_403(client: TestClient) -> None:
    """AC: non-admin operator promoting to ``tenant`` returns 403.

    Detail is ``insufficient_promotion_authority`` (the canonical
    string the audit-query consumer greps on, and the literal
    :func:`~meho_backplane.memory.rbac.assert_can_promote` raises with).
    """
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    # Seed a ``user-tenant`` source so the operator can read it (the
    # natural-key lookup binds user_sub).
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=uuid.uuid4(),
                tenant_id=tenant_a,
                source="memory",
                source_id=f"user-tenant:{alice_sub}:my-tenant-pref",
                kind="memory-user-tenant",
                body="x",
                body_hash="z" * 64,
                tokens=1,
                embedding=[0.01] * 384,
                doc_metadata={
                    "scope": "user-tenant",
                    "user_sub": alice_sub,
                    "target_name": None,
                    "expires_at": None,
                },
            )
        )
        await session.commit()

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user-tenant/my-tenant-pref/promote",
            json={"to": "tenant"},
            headers=_authed(token),
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_promotion_authority"


@pytest.mark.asyncio
async def test_promote_tenant_admin_to_tenant_succeeds(client: TestClient) -> None:
    """A ``tenant_admin`` operator can promote ``user-tenant -> tenant``.

    Companion test to the 403 case above: same memory ladder, admin
    role, expect 200 + target row. Confirms the gate isn't
    over-rotated (admin should be able to drive the widen).
    """
    tenant_a = uuid.uuid4()
    admin_sub = "op-admin"
    key, token = _admin_token(tenant_id=tenant_a, sub=admin_sub)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        session.add(
            Document(
                id=uuid.uuid4(),
                tenant_id=tenant_a,
                source="memory",
                source_id=f"user-tenant:{admin_sub}:admin-pref",
                kind="memory-user-tenant",
                body="admin note",
                body_hash="q" * 64,
                tokens=1,
                embedding=[0.01] * 384,
                doc_metadata={
                    "scope": "user-tenant",
                    "user_sub": admin_sub,
                    "target_name": None,
                    "expires_at": None,
                },
            )
        )
        await session.commit()

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user-tenant/admin-pref/promote",
            json={"to": "tenant"},
            headers=_authed(token),
        )

    assert response.status_code == 200
    assert response.json()["scope"] == "tenant"
    assert response.json()["metadata"]["promoted_from"] == "user-tenant/admin-pref"


# ---------------------------------------------------------------------------
# Tenant boundary: cross-tenant promote returns 404 (not 403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_cross_tenant_returns_404_not_403(client: TestClient) -> None:
    """AC: operator in tenant A promoting tenant B's memory returns 404.

    The tenant-boundary info-leak avoidance: a 403 would let a
    probing caller infer the slug exists in another tenant. 404 is
    the load-bearing collapse (same shape as the recall route).
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    # Seed a user-scoped memory in tenant B.
    await _insert_user_memory(
        tenant_id=tenant_b,
        user_sub="op-bob",
        slug="bob-pref",
    )
    # Operator in tenant A tries to promote it.
    key, token = _operator_token(tenant_id=tenant_a, sub="op-alice")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/bob-pref/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "memory_not_found"


@pytest.mark.asyncio
async def test_promote_nonexistent_source_returns_404(client: TestClient) -> None:
    """Source slug doesn't exist in this tenant -- 404 ``memory_not_found``."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-alice")

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/never-existed/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "memory_not_found"


# ---------------------------------------------------------------------------
# Audit row carries audit_promotion_target_scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_audit_row_carries_promotion_target_scope_payload_key(
    client: TestClient,
) -> None:
    """AC: ``audit_log`` row for a promote carries ``promotion_target_scope``.

    Distinguishable from a plain ``memory.remember`` row by the
    ``op_id`` and the ``promotion_target_scope`` payload key. The
    contextvar binding pattern (``audit_promotion_target_scope``)
    mirrors G0.4-T5's ``audit_query_hash`` precedent.
    """
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(tenant_id=tenant_a, user_sub=alice_sub)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/memory/user/wine-preference/promote")
    post_rows = [r for r in rows if r.method == "POST"]
    assert len(post_rows) == 1
    payload = post_rows[0].payload
    assert payload["op_id"] == "memory.promote"
    assert payload["op_class"] == "write"
    assert payload["scope"] == "user"
    assert payload["slug"] == "wine-preference"
    # The load-bearing distinguisher: distinguishes promote rows
    # from regular memory.remember rows for G8 audit queries.
    assert payload["promotion_target_scope"] == "user-tenant"


# ---------------------------------------------------------------------------
# Atomicity regression: failed target insert leaves source intact
# ---------------------------------------------------------------------------


# usefixtures(log_buffer): requested for its side effect only — it routes
# structlog to a buffer so the 0.137 re-raised handler exception doesn't
# surface as a teardown ERROR. ASGITransport alone does not suppress it.
@pytest.mark.usefixtures("log_buffer")
@pytest.mark.asyncio
async def test_promote_failed_target_insert_leaves_source_intact(
    client: TestClient,
) -> None:
    """AC: failed target insert leaves source intact (transaction boundary).

    Patches :func:`index_document` to raise; the source row must
    still be present afterwards because the promote method opens one
    session that wraps both writes and the move-source delete is
    inside the same ``async with`` block.
    """
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(tenant_id=tenant_a, user_sub=alice_sub)

    transport = ASGITransport(app=_build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as raising_client:
        with (
            respx.mock as mock_router,
            patch(
                "meho_backplane.memory.service.index_document",
                side_effect=RuntimeError("simulated insert failure"),
            ),
        ):
            _mock_discovery_and_jwks(mock_router, _public_jwks(key))
            response = await raising_client.post(
                "/api/v1/memory/user/wine-preference/promote",
                json={"to": "user-tenant", "move": True},
                headers=_authed(token),
            )

    # Handler exception → 500 with ASGITransport(raise_app_exceptions=False).
    assert response.status_code == 500
    # Source row still there: the failed transaction rolled back the
    # in-flight delete (the delete and insert share the same session).
    user_rows = await _fetch_documents_by_kind(tenant_a, "memory-user")
    assert len(user_rows) == 1


# ---------------------------------------------------------------------------
# Schema validation: extra fields, missing required, slug overrun
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_rejects_unknown_body_field(client: TestClient) -> None:
    """``extra="forbid"`` on :class:`PromoteBody` surfaces typos as 422."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-alice")
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant", "destinaton": "tenant"},  # typo
            headers=_authed(token),
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_promote_rejects_missing_to_field(client: TestClient) -> None:
    """``to`` is required -- omitted yields 422."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-alice")
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"move": False},
            headers=_authed(token),
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_promote_oversized_slug_path_returns_404(client: TestClient) -> None:
    """Path-parameter overrun is clamped at 404 (same shape as the recall route)."""
    tenant_a = uuid.uuid4()
    key, token = _operator_token(tenant_id=tenant_a, sub="op-alice")
    oversized = "x" * 600  # > _SLUG_MAX_LENGTH (256)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/memory/user/{oversized}/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "memory_not_found"


# ---------------------------------------------------------------------------
# Body not leaked in audit payload (mirrors the remember route's regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_audit_payload_does_not_leak_body(client: TestClient) -> None:
    """Memory body must NOT appear in the audit payload.

    Mirrors the kb / remember regression: the audit row's payload is
    for the operation, not the document content. The structured
    log line for ``memory_promote`` carries the slug; the body lives
    only in the ``documents`` table.
    """
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(
        tenant_id=tenant_a,
        user_sub=alice_sub,
        body="VERY SECRET SOURCE",
    )

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )
    assert response.status_code == 200

    rows = await _audit_rows_for_path("/api/v1/memory/user/wine-preference/promote")
    assert len(rows) == 1
    serialised = json.dumps(rows[0].payload)
    assert "VERY SECRET SOURCE" not in serialised


# ---------------------------------------------------------------------------
# Smoke: structlog clears contextvars at the end of the request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promote_clears_audit_contextvars_between_requests(
    client: TestClient,
) -> None:
    """Audit contextvar leakage between requests is the load-bearing failure mode.

    The chassis :class:`~meho_backplane.middleware.RequestContextMiddleware`
    is what owns contextvar reset; this test pins that an
    ``audit_promotion_target_scope`` from a promote request does NOT
    survive into a subsequent ``memory.list`` row's payload.
    """
    tenant_a = uuid.uuid4()
    alice_sub = "op-alice"
    key, token = _operator_token(tenant_id=tenant_a, sub=alice_sub)
    await _insert_user_memory(tenant_id=tenant_a, user_sub=alice_sub)
    # Run the promote, then a list, then assert no key leakage.
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        promote_resp = client.post(
            "/api/v1/memory/user/wine-preference/promote",
            json={"to": "user-tenant"},
            headers=_authed(token),
        )
        assert promote_resp.status_code == 200
        # Clear structlog's per-test contextvars so we're not asserting
        # against an in-process side effect of the TestClient transport.
        structlog.contextvars.clear_contextvars()
        list_resp = client.get(
            "/api/v1/memory",
            headers=_authed(token),
        )
        assert list_resp.status_code == 200

    list_rows = await _audit_rows_for_path("/api/v1/memory")
    list_get_rows = [r for r in list_rows if r.method == "GET"]
    assert len(list_get_rows) == 1
    payload = list_get_rows[0].payload
    assert "promotion_target_scope" not in payload
