# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the doc-collection update surface (#3601).

The in-place repoint half of the registry — the counterpart to the create
route (#1739) and the delete route (#2487). A migration-seeded collection
that carries its own ``backend.ref["endpoint"]`` could never have that
endpoint changed through a governed path (create 409s on the key, delete
refuses a global row, there was no PATCH), so a corpus move left it 503-ing.

Coverage matrix (Task #3601 acceptance criteria):

* **Service (:func:`update_doc_collection`)** — a backend repoint resets
  readiness (``status`` → ``provisioning``, cached liveness cleared) so a
  probe re-validates; a metadata-only change leaves ``status`` + liveness
  untouched; a repoint of a **disabled** collection stays disabled (operator
  intent wins); an identical backend is not treated as a change; a global row
  needs ``platform_admin``; a supplied backend runs the same ``backend.type``
  registry check + ``https`` / SSRF endpoint screen as create; ``ref={}``
  clears the endpoint (falls back to ``settings.corpus_url``).
* **REST route** — ``PATCH /api/v1/doc_collections/{key}`` → 200 + audit row
  ``meho.docs.collections.update``; 422 on an unknown backend type / bad
  endpoint; 403 ``global_collection_update_forbidden`` for a global row
  without ``platform_admin`` and 200 with it; 403 for a plain OPERATOR;
  404 (with ``known_keys``) for an unknown key; 422 for an empty body.

Runs against ``sqlite+aiosqlite`` via the shared engine the autouse
``_default_database_url`` conftest fixture pre-migrates to ``alembic upgrade
head`` — identical to :mod:`tests.test_doc_collections_delete`.
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
    DocCollectionBackendTypeError,
    DocCollectionEndpointError,
    DocCollectionGlobalUpdateForbiddenError,
    DocCollectionUpdate,
    update_doc_collection,
)
from meho_backplane.docs_collections.lifecycle import (
    STATUS_DISABLED,
    STATUS_PROVISIONING,
    STATUS_READY,
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
_NEW_CORPUS_URL = "https://corpus-new.test/v1/search"


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
    monkeypatch.delenv("MEHO_TARGET_SSRF_ALLOWLIST", raising=False)
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


def _make_operator(
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    platform_admin: bool = False,
) -> Operator:
    return Operator(
        sub="admin-1",
        tenant_id=uuid.UUID(tenant_id),
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
        capabilities=frozenset({"meho-docs"}),
        platform_admin=platform_admin,
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
        "status": STATUS_READY,
        "last_ingested_at": datetime(2026, 1, 1, tzinfo=UTC),
        "doc_count": 42,
        "readiness": {"reachable": True, "index_built": True},
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


async def _fetch_row(collection_key: str) -> DocCollectionORM:
    sm = get_sessionmaker()
    async with sm() as session:
        return (
            await session.execute(
                select(DocCollectionORM).where(DocCollectionORM.collection_key == collection_key)
            )
        ).scalar_one()


async def _run_update(row_id: uuid.UUID, operator: Operator, body: DocCollectionUpdate) -> None:
    sm = get_sessionmaker()
    async with sm() as session, session.begin():
        row = await session.get(DocCollectionORM, row_id)
        assert row is not None
        await update_doc_collection(session, operator, row, body)


# ---------------------------------------------------------------------------
# Service: backend repoint resets readiness; metadata-only does not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repoint_backend_resets_readiness_to_provisioning() -> None:
    """A backend change clears cached liveness and returns a live row to provisioning."""
    collection = await _insert_collection(status=STATUS_READY)
    await _run_update(
        collection.id,
        _make_operator(),
        DocCollectionUpdate(backend={"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}),
    )

    row = await _fetch_row("vmware")
    assert row.backend == {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}
    assert row.status == STATUS_PROVISIONING
    assert row.readiness is None
    assert row.doc_count is None
    assert row.last_ingested_at is None


@pytest.mark.asyncio
async def test_metadata_only_update_preserves_status_and_liveness() -> None:
    """Changing description alone leaves status + probe-written liveness untouched."""
    collection = await _insert_collection(status=STATUS_READY)
    await _run_update(
        collection.id,
        _make_operator(),
        DocCollectionUpdate(description="now with more detail", when_to_use="pick for vSphere"),
    )

    row = await _fetch_row("vmware")
    assert row.description == "now with more detail"
    assert row.when_to_use == "pick for vSphere"
    # Untouched: no backend change means no readiness reset.
    assert row.status == STATUS_READY
    assert row.readiness == {"reachable": True, "index_built": True}
    assert row.doc_count == 42


@pytest.mark.asyncio
async def test_repoint_of_disabled_collection_stays_disabled() -> None:
    """A repoint never silently re-enables a disabled collection (operator intent wins)."""
    collection = await _insert_collection(status=STATUS_DISABLED, readiness={"reachable": True})
    await _run_update(
        collection.id,
        _make_operator(),
        DocCollectionUpdate(backend={"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}),
    )

    row = await _fetch_row("vmware")
    # Backend moved and stale liveness is cleared, but the explicit disable holds.
    assert row.backend["ref"] == {"endpoint": _NEW_CORPUS_URL}
    assert row.status == STATUS_DISABLED
    assert row.readiness is None


@pytest.mark.asyncio
async def test_identical_backend_is_not_a_change() -> None:
    """Re-sending the current backend does not reset readiness (no spurious churn)."""
    collection = await _insert_collection(status=STATUS_READY)
    await _run_update(
        collection.id,
        _make_operator(),
        DocCollectionUpdate(backend={"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}}),
    )

    row = await _fetch_row("vmware")
    assert row.status == STATUS_READY
    assert row.readiness == {"reachable": True, "index_built": True}


@pytest.mark.asyncio
async def test_clear_ref_falls_back_to_global_corpus_url() -> None:
    """A ``ref={}`` repoint passes the screen (nothing to dial) and resets to provisioning."""
    collection = await _insert_collection(status=STATUS_READY)
    await _run_update(
        collection.id,
        _make_operator(),
        DocCollectionUpdate(backend={"type": "corpus-http", "ref": {}}),
    )

    row = await _fetch_row("vmware")
    assert row.backend == {"type": "corpus-http", "ref": {}}
    assert row.status == STATUS_PROVISIONING


# ---------------------------------------------------------------------------
# Service: validation + SSRF screen (same as create)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_unknown_backend_type_raises() -> None:
    collection = await _insert_collection()
    with pytest.raises(DocCollectionBackendTypeError):
        await _run_update(
            collection.id,
            _make_operator(),
            DocCollectionUpdate(backend={"type": "no-such-backend", "ref": {}}),
        )
    # Row untouched (the begin() block rolled back on the raise).
    row = await _fetch_row("vmware")
    assert row.backend == {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_endpoint",
    [
        "http://corpus.internal/v1/search",  # plaintext scheme
        "https://127.0.0.1/v1/search",  # loopback
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/v1/search",  # RFC 1918
    ],
)
async def test_update_endpoint_screen_rejects_non_public(bad_endpoint: str) -> None:
    collection = await _insert_collection()
    with pytest.raises(DocCollectionEndpointError):
        await _run_update(
            collection.id,
            _make_operator(),
            DocCollectionUpdate(backend={"type": "corpus-http", "ref": {"endpoint": bad_endpoint}}),
        )
    row = await _fetch_row("vmware")
    assert row.backend == {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}}


# ---------------------------------------------------------------------------
# Service: global-row platform-seat gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_row_update_forbidden_without_platform_admin() -> None:
    collection = await _insert_collection(tenant_id=None, status=STATUS_READY)
    with pytest.raises(DocCollectionGlobalUpdateForbiddenError) as exc:
        await _run_update(
            collection.id,
            _make_operator(platform_admin=False),
            DocCollectionUpdate(
                backend={"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}
            ),
        )
    assert exc.value.detail["error"] == "global_collection_update_forbidden"
    # The global row survives untouched.
    row = await _fetch_row("vmware")
    assert row.backend == {"type": "corpus-http", "ref": {"endpoint": _CORPUS_URL}}


@pytest.mark.asyncio
async def test_global_row_update_allowed_for_platform_admin() -> None:
    collection = await _insert_collection(tenant_id=None, status=STATUS_READY)
    await _run_update(
        collection.id,
        _make_operator(platform_admin=True),
        DocCollectionUpdate(backend={"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}),
    )
    row = await _fetch_row("vmware")
    assert row.backend["ref"] == {"endpoint": _NEW_CORPUS_URL}
    assert row.status == STATUS_PROVISIONING


# ---------------------------------------------------------------------------
# Schema: partial-update contract
# ---------------------------------------------------------------------------


def test_empty_update_body_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DocCollectionUpdate()


def test_null_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be cleared to null"):
        DocCollectionUpdate(backend=None)


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


def _admin_token(key: Any, *, platform_admin: bool | None = None) -> str:
    return mint_token(
        key,
        sub="admin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        platform_admin=platform_admin,
    )


def _operator_token(key: Any) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value)


def _patch(
    client: TestClient,
    key: Any,
    token: str,
    body: dict[str, Any],
    collection_key: str = "vmware",
) -> Any:
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.patch(
            f"/api/v1/doc_collections/{collection_key}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


async def _audit_rows() -> list[AuditLog]:
    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_update_route_repoints_and_audits(client: TestClient) -> None:
    """200 on a tenant row repoint; status → provisioning; audit row bound."""
    await _insert_collection(collection_key="vmware", status=STATUS_READY)
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _admin_token(key),
        {"backend": {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}
    assert body["status"] == "provisioning"
    assert body["readiness"] is None

    rows = await _audit_rows()
    update_rows = [r for r in rows if r.payload.get("op_id") == "meho.docs.collections.update"]
    assert len(update_rows) == 1, [r.payload.get("op_id") for r in rows]
    assert update_rows[0].payload["op_class"] == "write"
    assert update_rows[0].payload["op_id"].startswith("meho.docs.")


@pytest.mark.asyncio
async def test_update_route_unknown_backend_type_is_422(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware")
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _admin_token(key),
        {"backend": {"type": "no-such-backend", "ref": {}}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["kind"] == "unknown_backend_type"


@pytest.mark.asyncio
async def test_update_route_bad_endpoint_is_422(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware")
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _admin_token(key),
        {"backend": {"type": "corpus-http", "ref": {"endpoint": "https://169.254.169.254/x"}}},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["kind"] == "endpoint_not_allowed"


@pytest.mark.asyncio
async def test_update_route_global_row_without_platform_admin_403(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware", tenant_id=None, status=STATUS_READY)
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _admin_token(key),
        {"backend": {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "global_collection_update_forbidden"


@pytest.mark.asyncio
async def test_update_route_global_row_with_platform_admin_200(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware", tenant_id=None, status=STATUS_READY)
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _admin_token(key, platform_admin=True),
        {"backend": {"type": "corpus-http", "ref": {"endpoint": _NEW_CORPUS_URL}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["backend"]["ref"] == {"endpoint": _NEW_CORPUS_URL}


def test_update_route_requires_tenant_admin(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _operator_token(key),
        {"description": "x"},
    )
    assert resp.status_code == 403, resp.text


def test_update_route_unknown_key_404(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    resp = _patch(
        client,
        key,
        _admin_token(key),
        {"description": "x"},
        collection_key="nope",
    )
    assert resp.status_code == 404, resp.text
    assert "known_keys" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_update_route_empty_body_422(client: TestClient) -> None:
    await _insert_collection(collection_key="vmware")
    key = make_rsa_keypair("kid-A")
    resp = _patch(client, key, _admin_token(key), {})
    assert resp.status_code == 422, resp.text
