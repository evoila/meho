# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the doc-collection create surface (#1739).

Covers the create half added across REST + service (the MCP create tool is
covered in :mod:`tests.test_mcp_tools_doc_collections`), mirroring the
``create_target`` precedent's guardrails:

* **201 + full body** — a valid create returns the full ``DocCollection``
  with a server-generated ``id`` / timestamps and ``status="provisioning"``.
* **tenant_id from JWT, never the body** — a body that tries to smuggle a
  ``tenant_id`` is rejected by the schema (``extra="forbid"``); the created
  row's tenant is always the operator's.
* **unknown backend type → 422** (not a deferred 503), and the detail
  enumerates the registered backend types.
* **cross-scope key collision → 409** (not an opaque 500 / IntegrityError).
* **non-tenant_admin → 403** (parity with enable / disable).
* **audit** — the create writes one ``audit_log`` row with
  ``op_id="meho.docs.collections.create"`` / ``op_class="write"``,
  joinable under an ``op_id="meho.docs.*"`` filter.

Runs against ``sqlite+aiosqlite`` via the shared pre-migrated engine, the
same harness :mod:`tests.test_doc_collections_readiness` uses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker, reset_engine_for_testing
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import DocCollection as DocCollectionORM
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


def _valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "collection_key": "vmware",
        "vendor": "VMware by Broadcom",
        "products": ["vsphere", "nsx"],
        "when_to_use": "Use for VMware product questions.",
        "backend": {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}},
    }
    body.update(overrides)
    return body


def _post(client: TestClient, key: Any, token: str, body: dict[str, Any]) -> Any:
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.post(
            "/api/v1/doc_collections",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


async def _fetch_row(collection_key: str) -> DocCollectionORM:
    sm = get_sessionmaker()
    async with sm() as session:
        return (
            await session.execute(
                select(DocCollectionORM).where(DocCollectionORM.collection_key == collection_key)
            )
        ).scalar_one()


async def _audit_rows() -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_create_returns_201_with_full_collection(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _post(client, key, _admin_token(key), _valid_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["collection_key"] == "vmware"
    assert body["vendor"] == "VMware by Broadcom"
    assert body["products"] == ["vsphere", "nsx"]
    # Server-derived fields.
    assert uuid.UUID(body["id"])
    assert body["status"] == "provisioning"
    assert body["last_ingested_at"] is None
    assert body["doc_count"] is None
    # The backend record round-trips (it is part of the full read shape).
    assert body["backend"] == {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}}


@pytest.mark.asyncio
async def test_create_tenant_id_comes_from_jwt_not_body(client: TestClient) -> None:
    """A body smuggling a different tenant_id cannot override the JWT's."""
    key = make_rsa_keypair("kid-A")
    foreign_tenant = str(uuid.uuid4())
    body = _valid_body(tenant_id=foreign_tenant)
    resp = _post(client, key, _admin_token(key), body)
    # extra="forbid" rejects the unmodelled tenant_id field outright (422).
    assert resp.status_code == 422, resp.text

    # And a clean create lands on the operator's tenant, never a body value.
    resp_ok = _post(client, key, _admin_token(key), _valid_body())
    assert resp_ok.status_code == 201, resp_ok.text
    row = await _fetch_row("vmware")
    assert str(row.tenant_id) == DEFAULT_TENANT_ID


# ---------------------------------------------------------------------------
# Validation: unknown backend type → 422 (not a deferred 503)
# ---------------------------------------------------------------------------


def test_unknown_backend_type_is_422_listing_registered_types(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    body = _valid_body(backend={"type": "no-such-backend", "ref": {}})
    resp = _post(client, key, _admin_token(key), body)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["kind"] == "unknown_backend_type"
    assert detail["backend_type"] == "no-such-backend"
    # The detail enumerates the registered backend types (from all_backends()).
    assert "corpus-http" in detail["valid_backend_types"]


# ---------------------------------------------------------------------------
# Conflict: duplicate key in the same scope → 409 (not an opaque 500)
# ---------------------------------------------------------------------------


def test_duplicate_collection_key_in_scope_is_409(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    first = _post(client, key, _admin_token(key), _valid_body())
    assert first.status_code == 201, first.text
    second = _post(client, key, _admin_token(key), _valid_body())
    assert second.status_code == 409, second.text


# ---------------------------------------------------------------------------
# RBAC: non-tenant_admin → 403 (parity with enable / disable)
# ---------------------------------------------------------------------------


def test_create_requires_tenant_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _post(client, key, _operator_token(key), _valid_body())
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_writes_audit_row_with_canonical_op_id(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _post(client, key, _admin_token(key), _valid_body())
    assert resp.status_code == 201, resp.text

    rows = await _audit_rows()
    create_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.create"]
    assert len(create_rows) == 1, [r.payload.get("op_id") for r in rows]
    assert create_rows[0].payload["op_class"] == "write"
    # Joinable under the meho.docs.* who-touched filter.
    assert create_rows[0].payload["op_id"].startswith("meho.docs.")
