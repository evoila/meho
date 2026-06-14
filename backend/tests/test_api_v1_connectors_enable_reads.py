# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route tests for ``POST /api/v1/connectors/{id}/enable-reads`` (G0.25-T7 #1749).

The service-layer behaviour matrix lives in
:mod:`tests.test_operations_ingest_review` (the ``enable_reads`` block);
this module pins the REST surface on top of it:

* 200 + ``{connector_id, ops_enabled}`` on success; only the read-class
  (GET/HEAD) ingested ops flip, every write-shaped verb stays
  default-deny.
* ``tenant_admin`` role required — an ``operator``-role JWT gets 403
  before the service layer runs.
* The route always scopes to the calling operator's tenant (#1699
  contract: no ``tenant_id`` parameter), so another tenant's rows 404
  and survive untouched.
* Idempotent: a second call returns ``ops_enabled=0``.
* Unknown connector → 404.

Self-contained rather than appended to
``test_api_v1_connectors_ingest.py``: the enable-reads surface needs
only a sliver of that module's machinery (JWT mint + discovery/JWKS
mocks), and the file mirrors ``test_api_v1_connectors_delete.py``'s
shape so the two route-test modules read the same.
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

from meho_backplane.api.v1.connectors_ingest import (
    router as connectors_ingest_router,
)
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_PRODUCT = "vmware"
_VERSION = "9.0"
_IMPL_ID = "vmware-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

#: One op per HTTP verb. The read class (GET / HEAD) flips; the
#: write-shaped verbs stay default-deny.
_INGESTED_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
_READ_METHODS = frozenset({"GET", "HEAD"})


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    """Empty the module-level JWKS cache around every test."""
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(connectors_ingest_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _token(*, tenant_id: uuid.UUID, role: str = "tenant_admin") -> tuple[Any, str]:
    """Mint a (private_key, JWT) pair for *tenant_id* with *role*."""
    key = _make_rsa_keypair("kid-enable-reads")
    token = _mint_token(
        key,
        sub=f"op-{role}",
        tenant_id=str(tenant_id),
        tenant_role=role,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_rows(*, tenant_id: uuid.UUID) -> None:
    """Seed one group with one ingested op per HTTP verb under *tenant_id*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_id = uuid.uuid4()
        session.add(
            OperationGroup(
                id=group_id,
                tenant_id=tenant_id,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                group_key="resources",
                name="Resources",
                when_to_use="Use for resource ops.",
                review_status="staged",
            ),
        )
        for method in _INGESTED_METHODS:
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=f"{method}:/api/v1/resource",
                    source_kind="ingested",
                    method=method,
                    path="/api/v1/resource",
                    summary=f"{method} resource",
                    group_id=group_id,
                    tags=["test"],
                    parameter_schema={"type": "object"},
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=False,
                ),
            )
        await session.commit()


async def _ops_enabled_state(tenant_id: uuid.UUID | None) -> dict[str, bool]:
    """Return ``{op_id: is_enabled}`` for every op under the test triple."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == _PRODUCT,
            EndpointDescriptor.version == _VERSION,
            EndpointDescriptor.impl_id == _IMPL_ID,
        )
        if tenant_id is None:
            stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            stmt = stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return {op.op_id: op.is_enabled for op in result.scalars().all()}


async def _enable_reads_audit_count() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.method == "SERVICE",
                AuditLog.path == "meho.connector.enable_reads",
            ),
        )
        return len(list(result.scalars().all()))


# ---------------------------------------------------------------------------
# Route behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_reads_flips_reads_only_returns_count(client: TestClient) -> None:
    """Happy path: 200 + ops_enabled=2; GET/HEAD flip, writes stay default-deny."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant)
    await _seed_rows(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads",
            headers=_authed(token),
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"connector_id": _CONNECTOR_ID, "ops_enabled": 2}

    state = await _ops_enabled_state(tenant)
    for op_id, enabled in state.items():
        method = op_id.split(":", 1)[0]
        if method in _READ_METHODS:
            assert enabled is True, f"read-class {op_id} should be enabled"
        else:
            assert enabled is False, f"write-class {op_id} must stay default-deny"
    assert await _enable_reads_audit_count() == 1


@pytest.mark.asyncio
async def test_enable_reads_is_idempotent(client: TestClient) -> None:
    """A second call returns ops_enabled=0 and writes no second audit row."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant)
    await _seed_rows(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        first = client.post(
            f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads",
            headers=_authed(token),
        )
        assert first.json()["ops_enabled"] == 2

        second = client.post(
            f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads",
            headers=_authed(token),
        )
        assert second.status_code == 200
        assert second.json()["ops_enabled"] == 0

    assert await _enable_reads_audit_count() == 1


@pytest.mark.asyncio
async def test_enable_reads_requires_tenant_admin_role(client: TestClient) -> None:
    """An operator-role JWT is rejected with 403 before the service runs."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant, role="operator")
    await _seed_rows(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads",
            headers=_authed(token),
        )

    assert response.status_code == 403
    state = await _ops_enabled_state(tenant)
    assert not any(state.values()), "no op should have flipped on a 403"


@pytest.mark.asyncio
async def test_enable_reads_is_tenant_scoped_cross_tenant_404(client: TestClient) -> None:
    """Another tenant's rows are invisible: 404, nothing flips (#1699 contract)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    key, token = _token(tenant_id=tenant_a)
    await _seed_rows(tenant_id=tenant_b)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads",
            headers=_authed(token),
        )

    assert response.status_code == 404
    state = await _ops_enabled_state(tenant_b)
    assert not any(state.values()), "tenant B's reads must survive untouched"


@pytest.mark.asyncio
async def test_enable_reads_unknown_connector_404(client: TestClient) -> None:
    """No rows for the triple under the operator's tenant → 404."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            f"/api/v1/connectors/{_CONNECTOR_ID}/enable-reads",
            headers=_authed(token),
        )

    assert response.status_code == 404
