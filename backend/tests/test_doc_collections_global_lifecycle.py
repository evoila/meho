# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Enable, disable and probe of a global doc collection need a platform admin.

A global collection (``tenant_id IS NULL``) is shared by every tenant that
has no own copy of the key. These tests pin the rule on the service and on
the REST routes:

* A tenant admin without ``platform_admin`` gets
  :class:`DocCollectionGlobalUpdateForbiddenError` (REST 403
  ``global_collection_update_forbidden``) on enable, disable and probe of a
  global row, and the row does not change. The probe backend is not called.
* A platform admin can enable, disable and probe a global row.
* A tenant admin can still enable and disable their own tenant's row, also
  when it has the same key as a global row (the global row stays as it is).

The console routes are covered in ``test_ui_corpus_collection_detail``.
"""

from __future__ import annotations

import functools
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
from meho_backplane.db.models import DocCollection as DocCollectionORM
from meho_backplane.docs_collections import (
    DocCollectionGlobalUpdateForbiddenError,
    probe_collection,
    set_collection_enabled,
)
from meho_backplane.docs_collections.lifecycle import (
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    STATUS_READY,
)
from meho_backplane.docs_search.backends import BackendReadiness, CorpusHttpBackend
from meho_backplane.docs_search.backends.base import SearchBackend
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
_FORBIDDEN = "global_collection_update_forbidden"


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


@pytest.fixture
def probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the corpus probe with a stub that records each call."""
    calls: list[str] = []

    async def _fake_probe(
        self: SearchBackend, operator: Operator, *, backend_ref: Any = None
    ) -> BackendReadiness:
        calls.append(operator.sub)
        return BackendReadiness(reachable=True, index_built=True, doc_count=7)

    monkeypatch.setattr(CorpusHttpBackend, "probe", _fake_probe)
    return calls


def _make_operator(*, platform_admin: bool = False) -> Operator:
    return Operator(
        sub="admin-1",
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID),
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
        capabilities=frozenset({"meho-docs"}),
        platform_admin=platform_admin,
    )


async def _insert_collection(**kwargs: Any) -> DocCollectionORM:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": None,
        "collection_key": "vmware",
        "vendor": "VMware",
        "products": ["vsphere"],
        "description": None,
        "when_to_use": None,
        "backend": {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}},
        "status": STATUS_READY,
        "last_ingested_at": None,
        "doc_count": 42,
        "readiness": None,
        "extras": {},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(kwargs)
    row = DocCollectionORM(**defaults)
    sm = get_sessionmaker()
    async with sm() as session:
        session.add(row)
        await session.commit()
    return row


async def _fetch(row_id: uuid.UUID) -> DocCollectionORM:
    sm = get_sessionmaker()
    async with sm() as session:
        row = (
            await session.execute(select(DocCollectionORM).where(DocCollectionORM.id == row_id))
        ).scalar_one()
        return row


def _assert_unchanged(row: DocCollectionORM, *, status: str) -> None:
    assert row.status == status
    assert row.doc_count == 42
    assert row.readiness is None
    assert row.updated_at.replace(tzinfo=UTC) == datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start_status", "enabled"),
    [
        (STATUS_READY, False),  # disable a live global row
        (STATUS_DISABLED, True),  # enable a global row a platform admin turned off
        (STATUS_DISABLED, False),  # already at target: still refused
    ],
)
async def test_tenant_admin_cannot_toggle_global_row(start_status: str, enabled: bool) -> None:
    collection = await _insert_collection(status=start_status)
    sm = get_sessionmaker()
    with pytest.raises(DocCollectionGlobalUpdateForbiddenError) as exc:
        async with sm() as session, session.begin():
            row = await session.get(DocCollectionORM, collection.id)
            assert row is not None
            await set_collection_enabled(session, _make_operator(), row, enabled=enabled)
    assert exc.value.detail["error"] == _FORBIDDEN
    _assert_unchanged(await _fetch(collection.id), status=start_status)


@pytest.mark.asyncio
async def test_platform_admin_can_toggle_global_row() -> None:
    collection = await _insert_collection(status=STATUS_READY)
    operator = _make_operator(platform_admin=True)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollectionORM, collection.id)
        assert row is not None
        assert await set_collection_enabled(session, operator, row, enabled=False) is True
    assert (await _fetch(collection.id)).status == STATUS_DISABLED
    async with sm() as session, session.begin():
        row = await session.get(DocCollectionORM, collection.id)
        assert row is not None
        assert await set_collection_enabled(session, operator, row, enabled=True) is True
    assert (await _fetch(collection.id)).status == STATUS_PROVISIONING


@pytest.mark.asyncio
async def test_tenant_admin_can_toggle_own_tenant_row() -> None:
    collection = await _insert_collection(
        tenant_id=uuid.UUID(DEFAULT_TENANT_ID), status=STATUS_READY
    )
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollectionORM, collection.id)
        assert row is not None
        assert await set_collection_enabled(session, _make_operator(), row, enabled=False) is True
    assert (await _fetch(collection.id)).status == STATUS_DISABLED


@pytest.mark.asyncio
async def test_tenant_admin_cannot_probe_global_row(probe_calls: list[str]) -> None:
    collection = await _insert_collection(status=STATUS_PROVISIONING)
    sm = get_sessionmaker()
    with pytest.raises(DocCollectionGlobalUpdateForbiddenError):
        async with sm() as session, session.begin():
            row = await session.get(DocCollectionORM, collection.id)
            assert row is not None
            await probe_collection(session, _make_operator(), row)
    # Refused before the backend is called; the row keeps its status.
    assert probe_calls == []
    _assert_unchanged(await _fetch(collection.id), status=STATUS_PROVISIONING)


@pytest.mark.asyncio
async def test_platform_admin_can_probe_global_row(probe_calls: list[str]) -> None:
    collection = await _insert_collection(status=STATUS_PROVISIONING)
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollectionORM, collection.id)
        assert row is not None
        await probe_collection(session, _make_operator(platform_admin=True), row)
    assert probe_calls == ["admin-1"]
    row_after = await _fetch(collection.id)
    assert row_after.status == STATUS_READY
    assert row_after.doc_count == 7


# ---------------------------------------------------------------------------
# REST routes
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


@functools.cache
def _signing_key() -> Any:
    """One key for the whole module, so the cached JWKS always matches."""
    return make_rsa_keypair("kid-A")


def _post(client: TestClient, path: str, *, platform_admin: bool | None = None) -> Any:
    key = _signing_key()
    token = mint_token(
        key,
        sub="admin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=platform_admin,
    )
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.post(path, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "start_status"),
    [
        ("disable", STATUS_READY),
        ("enable", STATUS_DISABLED),
        ("probe", STATUS_PROVISIONING),
    ],
)
async def test_route_tenant_admin_global_row_403(
    client: TestClient, probe_calls: list[str], verb: str, start_status: str
) -> None:
    collection = await _insert_collection(status=start_status)
    resp = _post(client, f"/api/v1/doc_collections/vmware/{verb}")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == _FORBIDDEN
    assert resp.json()["detail"]["collection_key"] == "vmware"
    assert probe_calls == []
    _assert_unchanged(await _fetch(collection.id), status=start_status)


@pytest.mark.asyncio
async def test_route_platform_admin_global_row_allowed(
    client: TestClient, probe_calls: list[str]
) -> None:
    collection = await _insert_collection(status=STATUS_READY)
    disable = _post(client, "/api/v1/doc_collections/vmware/disable", platform_admin=True)
    assert disable.status_code == 204, disable.text
    assert (await _fetch(collection.id)).status == STATUS_DISABLED

    enable = _post(client, "/api/v1/doc_collections/vmware/enable", platform_admin=True)
    assert enable.status_code == 204, enable.text
    assert (await _fetch(collection.id)).status == STATUS_PROVISIONING

    probe = _post(client, "/api/v1/doc_collections/vmware/probe", platform_admin=True)
    assert probe.status_code == 200, probe.text
    assert probe_calls == ["admin-1"]
    assert (await _fetch(collection.id)).status == STATUS_READY


@pytest.mark.asyncio
async def test_route_tenant_admin_own_copy_of_global_key(client: TestClient) -> None:
    """A tenant row with the same key as a global row: the tenant row changes, the global not."""
    global_row = await _insert_collection(status=STATUS_READY)
    tenant_row = await _insert_collection(
        id=uuid.uuid4(), tenant_id=uuid.UUID(DEFAULT_TENANT_ID), status=STATUS_READY
    )
    disable = _post(client, "/api/v1/doc_collections/vmware/disable")
    assert disable.status_code == 204, disable.text
    enable = _post(client, "/api/v1/doc_collections/vmware/enable")
    assert enable.status_code == 204, enable.text

    assert (await _fetch(tenant_row.id)).status == STATUS_PROVISIONING
    _assert_unchanged(await _fetch(global_row.id), status=STATUS_READY)
