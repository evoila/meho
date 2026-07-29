# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the doc-collection delete surface (#2487).

The delete half of the registry — the deregister counterpart to the create
route (#1739). Coverage matrix (Task #2487 acceptance criteria):

* **Service guards** — :func:`delete_doc_collection` refuses a global
  (``tenant_id IS NULL``) row with :class:`DocCollectionGlobalError` and a
  non-``disabled`` row with :class:`DocCollectionNotDisabledError`; the
  global guard precedes the disabled guard. A disabled, tenant-owned row is
  hard-deleted.
* **REST route** — ``DELETE /api/v1/doc_collections/{key}`` → 204 on a
  disabled tenant-owned row (row gone; audit row
  ``meho.docs.collections.delete``; the freed key re-``POST``s 201), 409
  ``collection_not_disabled`` for a non-disabled row, 403 ``global_collection``
  for a global row, 404 (with ``known_keys``) for an unknown key, and 403
  for a plain OPERATOR (tenant_admin-gated).
* **Tenant-shadow** — deleting a tenant row that shadows a global key
  un-shadows the global row (the resolver is tenant-first).

Runs against ``sqlite+aiosqlite`` via the shared engine the autouse
``_default_database_url`` conftest fixture pre-migrates to ``alembic
upgrade head`` — identical to :mod:`tests.test_doc_collections_readiness`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import (
    DocCollectionGlobalError,
    DocCollectionNotDisabledError,
    delete_doc_collection,
)
from meho_backplane.docs_collections.lifecycle import (
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    STATUS_READY,
    STATUS_REBUILDING,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE as _AUDIENCE,
)
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import (
    ISSUER as _ISSUER,
)

_CORPUS_URL = "https://corpus.test/v1/search"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` requires + a configured corpus."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    monkeypatch.setenv("CORPUS_URL", _CORPUS_URL)
    monkeypatch.setenv("CORPUS_AUDIENCE", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _make_operator(tenant_id: str = DEFAULT_TENANT_ID) -> Operator:
    return Operator(
        sub="admin-1",
        tenant_id=uuid.UUID(tenant_id),
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
        capabilities=frozenset({"meho-docs"}),
    )


async def _insert_collection(**kwargs: Any) -> DocCollectionORM:
    """Insert a DocCollection row via the test sessionmaker."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(DEFAULT_TENANT_ID),
        "collection_key": "vmware",
        "vendor": "VMware",
        "products": ["vsphere"],
        "description": None,
        "when_to_use": None,
        "backend": {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}},
        "status": STATUS_DISABLED,
        "last_ingested_at": None,
        "doc_count": None,
        "readiness": None,
        "extras": {},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    c = DocCollectionORM(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(c)
        await session.commit()
    return c


async def _rows_for_key(collection_key: str) -> list[DocCollectionORM]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(DocCollectionORM).where(DocCollectionORM.collection_key == collection_key)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Service guards (direct delete_doc_collection)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_disabled_tenant_row() -> None:
    collection = await _insert_collection(status=STATUS_DISABLED)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollectionORM, collection.id)
        assert row is not None
        await delete_doc_collection(session, _make_operator(), row)

    assert await _rows_for_key("vmware") == []


@pytest.mark.asyncio
async def test_delete_refuses_global_row_row_survives() -> None:
    """A global (tenant_id IS NULL) row is platform-owned — refuse + survive."""
    collection = await _insert_collection(tenant_id=None, status=STATUS_DISABLED)
    sm = get_sessionmaker()
    with pytest.raises(DocCollectionGlobalError) as exc:
        async with sm() as session, session.begin():
            row = await session.get(DocCollectionORM, collection.id)
            assert row is not None
            await delete_doc_collection(session, _make_operator(), row)
    assert exc.value.detail["error"] == "global_collection"

    # The row survives (the begin() block rolled back on the raise).
    assert len(await _rows_for_key("vmware")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [STATUS_PROVISIONING, STATUS_READY, STATUS_REBUILDING])
async def test_delete_refuses_non_disabled_row(status: str) -> None:
    collection = await _insert_collection(status=status)
    sm = get_sessionmaker()
    with pytest.raises(DocCollectionNotDisabledError) as exc:
        async with sm() as session, session.begin():
            row = await session.get(DocCollectionORM, collection.id)
            assert row is not None
            await delete_doc_collection(session, _make_operator(), row)
    assert exc.value.detail["error"] == "collection_not_disabled"
    assert exc.value.detail["status"] == status

    assert len(await _rows_for_key("vmware")) == 1


@pytest.mark.asyncio
async def test_global_guard_precedes_disabled_guard() -> None:
    """A global row that is also non-disabled fails the global guard first."""
    collection = await _insert_collection(tenant_id=None, status=STATUS_READY)
    sm = get_sessionmaker()
    with pytest.raises(DocCollectionGlobalError):
        async with sm() as session, session.begin():
            row = await session.get(DocCollectionORM, collection.id)
            assert row is not None
            await delete_doc_collection(session, _make_operator(), row)


# ---------------------------------------------------------------------------
# REST route
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    from meho_backplane.api.v1.doc_collections import router as doc_collections_router

    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(doc_collections_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_engine_for_testing()
    yield TestClient(_build_app())


def _admin_token(key: Any) -> str:
    return mint_token(key, sub="admin-1", tenant_role=TenantRole.TENANT_ADMIN.value)


def _operator_token(key: Any) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)


async def _audit_rows() -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_delete_route_frees_key_and_audits(client: TestClient) -> None:
    """204 on a disabled tenant row; row gone; audit bound; the key re-creates."""
    await _insert_collection(collection_key="vmware", status=STATUS_DISABLED)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.request(
            "DELETE",
            "/api/v1/doc_collections/vmware",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 204, resp.text
    assert await _rows_for_key("vmware") == []

    rows = await _audit_rows()
    delete_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.delete"]
    assert len(delete_rows) == 1, [r.payload.get("op_id") for r in rows]
    assert delete_rows[0].payload["op_class"] == "write"

    # The freed key re-creates 201 — the recovery loop that motivated #2487.
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        recreate = client.post(
            "/api/v1/doc_collections",
            json={
                "collection_key": "vmware",
                "vendor": "VMware by Broadcom",
                "backend": {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}},
            },
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert recreate.status_code == 201, recreate.text


@pytest.mark.asyncio
async def test_delete_route_enabled_collection_409(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware", status=STATUS_READY)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.request(
            "DELETE",
            "/api/v1/doc_collections/vmware",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "collection_not_disabled"
    assert detail["status"] == STATUS_READY
    # Untouched.
    assert len(await _rows_for_key("vmware")) == 1


@pytest.mark.asyncio
async def test_delete_route_global_collection_403(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware", tenant_id=None, status=STATUS_DISABLED)
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.request(
            "DELETE",
            "/api/v1/doc_collections/vmware",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "global_collection"
    assert len(await _rows_for_key("vmware")) == 1


def test_delete_route_unknown_key_404(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.request(
            "DELETE",
            "/api/v1/doc_collections/nope",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 404, resp.text
    assert "known_keys" in resp.json()["detail"]


def test_delete_route_requires_tenant_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.request(
            "DELETE",
            "/api/v1/doc_collections/vmware",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_tenant_shadow_unshadows_global(client: TestClient) -> None:
    """Deleting a tenant row that shadows a global key leaves the global row."""
    # A global 'vmware' (ready) plus a tenant-curated 'vmware' (disabled) that
    # shadows it. The resolver is tenant-first, so DELETE targets the tenant row.
    await _insert_collection(collection_key="vmware", tenant_id=None, status=STATUS_READY)
    await _insert_collection(
        collection_key="vmware", tenant_id=uuid.UUID(DEFAULT_TENANT_ID), status=STATUS_DISABLED
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        resp = client.request(
            "DELETE",
            "/api/v1/doc_collections/vmware",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert resp.status_code == 204, resp.text

    remaining = await _rows_for_key("vmware")
    assert len(remaining) == 1
    # The global row survived; the shadowing tenant row is gone.
    assert remaining[0].tenant_id is None
    assert remaining[0].status == STATUS_READY
