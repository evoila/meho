# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route tests for ``DELETE /api/v1/connectors/{connector_id}`` (G0.25-T2 #1700).

The service-layer behaviour matrix lives in
:mod:`tests.test_operations_ingest_delete`; this module pins the REST
surface on top of it:

* 204 on success; the response carries no body (the enabled-ops
  advisory's structured wire home is the MCP sibling — the REST path
  logs + audits instead).
* ``tenant_admin`` role required — an ``operator``-role JWT gets 403
  before the service layer runs.
* The route always scopes to the calling operator's tenant (#1699
  contract: no ``tenant_id`` parameter), so another tenant's rows
  404 and survive.
* Repeat DELETE collapses to 404 once the first one landed.
* End-to-end consumer scenario (the task's AC 8): ingest a zero-op
  spec through ``POST /api/v1/connectors/ingest``, watch the stub
  appear as a ``state="registered"`` row in ``GET
  /api/v1/connectors``, DELETE it, and verify it is gone from the
  listing, the v2 registry, and that the ``meho.connector.delete``
  audit row landed.

Self-contained rather than appended to
``test_api_v1_connectors_ingest.py``: the DELETE surface needs only a
sliver of that module's machinery (JWT mint + discovery/JWKS mocks +
one spec mock), and the sibling #1699 PR is appending to that file in
the same wave.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1.connectors_ingest import (
    router as connectors_ingest_router,
)
from meho_backplane.api.v1.connectors_ingest import (
    set_llm_client_factory,
)
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest import (
    default_llm_client_factory,
    ensure_connector_class_registered,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

_PRODUCT = "ghostspec"
_VERSION = "1.0"
_IMPL_ID = "ghostspec-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

# Public IP for the SSRF destination guard (IANA example.com assignment)
# + the hostnames this module's mocks serve. Mirrors
# test_api_v1_connectors_ingest.py.
_PUBLIC_IP = "93.184.216.34"
_SPEC_HOST = "specs.example.test"
_TEST_HOSTS = frozenset({_SPEC_HOST, "keycloak.test", "vault.test"})


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


def _getaddrinfo(
    host: str, port: object, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    if host in _TEST_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (_PUBLIC_IP, 443))]
    return socket.getaddrinfo(host, port, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _mock_ssrf_getaddrinfo() -> Iterator[None]:
    """Resolve the mock spec host to a public IP for the SSRF guard."""
    with patch(
        "meho_backplane.operations.ingest.openapi.socket.getaddrinfo",
        side_effect=_getaddrinfo,
    ):
        yield


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
    key = _make_rsa_keypair("kid-delete")
    token = _mint_token(
        key,
        sub=f"op-{role}",
        tenant_id=str(tenant_id),
        tenant_role=role,
    )
    return key, token


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_rows(*, tenant_id: uuid.UUID, op_is_enabled: bool = False) -> None:
    """Seed one group + two child ops for the test triple under *tenant_id*."""
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
                group_key="things",
                name="Things",
                when_to_use="Use for thing management.",
                review_status="staged",
            ),
        )
        for index in range(2):
            session.add(
                EndpointDescriptor(
                    tenant_id=tenant_id,
                    product=_PRODUCT,
                    version=_VERSION,
                    impl_id=_IMPL_ID,
                    op_id=f"GET:/things/{index}",
                    source_kind="ingested",
                    method="GET",
                    path=f"/things/{index}",
                    summary="Summary",
                    description="Description",
                    group_id=group_id,
                    tags=["test"],
                    parameter_schema={"type": "object"},
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=op_is_enabled,
                ),
            )
        await session.commit()


async def _row_counts(tenant_id: uuid.UUID | None) -> tuple[int, int]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        group_stmt = select(OperationGroup).where(
            OperationGroup.product == _PRODUCT,
            OperationGroup.version == _VERSION,
            OperationGroup.impl_id == _IMPL_ID,
        )
        op_stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == _PRODUCT,
            EndpointDescriptor.version == _VERSION,
            EndpointDescriptor.impl_id == _IMPL_ID,
        )
        if tenant_id is None:
            group_stmt = group_stmt.where(OperationGroup.tenant_id.is_(None))
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            group_stmt = group_stmt.where(OperationGroup.tenant_id == tenant_id)
            op_stmt = op_stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        groups = len((await session.execute(group_stmt)).scalars().all())
        ops = len((await session.execute(op_stmt)).scalars().all())
        return groups, ops


async def _service_delete_audit_count() -> int:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(
                AuditLog.method == "SERVICE",
                AuditLog.path == "meho.connector.delete",
            ),
        )
        return len(list(result.scalars().all()))


# ---------------------------------------------------------------------------
# Route behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_returns_204_then_404_on_repeat(client: TestClient) -> None:
    """Happy path: 204 + rows gone + shim gone; the second DELETE 404s."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant)
    ensure_connector_class_registered(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        base_url=None,
    )
    await _seed_rows(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}", headers=_authed(token))
        assert response.status_code == 204, response.text
        assert response.content == b""

        repeat = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}", headers=_authed(token))
        assert repeat.status_code == 404

    assert await _row_counts(tenant) == (0, 0)
    assert (_PRODUCT, _VERSION, _IMPL_ID) not in all_connectors_v2()
    assert await _service_delete_audit_count() == 1


@pytest.mark.asyncio
async def test_delete_requires_tenant_admin_role(client: TestClient) -> None:
    """An operator-role JWT is rejected with 403 before the service runs."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant, role="operator")
    await _seed_rows(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}", headers=_authed(token))

    assert response.status_code == 403
    assert await _row_counts(tenant) == (1, 2)


@pytest.mark.asyncio
async def test_delete_is_tenant_scoped_cross_tenant_404(client: TestClient) -> None:
    """Another tenant's rows are invisible: 404, nothing deleted (#1699 contract)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    key, token = _token(tenant_id=tenant_a)
    await _seed_rows(tenant_id=tenant_b)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}", headers=_authed(token))

    assert response.status_code == 404
    assert await _row_counts(tenant_b) == (1, 2)


@pytest.mark.asyncio
async def test_delete_with_enabled_ops_completes_with_204(client: TestClient) -> None:
    """Enabled ops do not block the REST delete — advisory is log/audit-side."""
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant)
    await _seed_rows(tenant_id=tenant, op_is_enabled=True)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.delete(f"/api/v1/connectors/{_CONNECTOR_ID}", headers=_authed(token))

    assert response.status_code == 204
    assert await _row_counts(tenant) == (0, 0)


# ---------------------------------------------------------------------------
# End-to-end consumer scenario (task AC 8)
# ---------------------------------------------------------------------------

_ZERO_OP_SPEC = """openapi: 3.0.3
info:
  title: ghost
  version: '1'
paths: {}
"""


class _NeverCalledLlmClient:
    """LLM stub that fails the test if the grouping pass ever invokes it.

    The pipeline resolves the LLM-client factory before it discovers
    there is nothing to group, so a zero-op ingest still needs a
    factory installed — but the client itself must never run (zero
    unassigned ops short-circuits ``run_llm_grouping``).
    """

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        raise AssertionError("zero-op ingest must not invoke the grouping LLM")


@pytest.fixture
def _stub_llm_factory() -> Iterator[None]:
    """Install the never-called stub; restore the fail-closed default after."""
    set_llm_client_factory(lambda: _NeverCalledLlmClient())
    yield
    set_llm_client_factory(default_llm_client_factory)


def _connector_ids(list_response: httpx.Response) -> set[str]:
    assert list_response.status_code == 200, list_response.text
    return {item["connector_id"] for item in list_response.json()["items"]}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_llm_factory")
async def test_e2e_zero_op_ingest_then_delete_clears_catalog(client: TestClient) -> None:
    """Ingest a zero-op spec, DELETE the stub, verify it left every surface.

    The exact claude-rdc-hetzner-dc v0.14.0 cycle-10 sequence: a spec
    whose ``paths`` is empty parses cleanly, registers the auto-shim
    (pre-flight + registration run before the upsert loop), and lands
    zero rows — leaving a ``state="registered"`` stub in the catalog
    that nothing could remove. ``run_llm_grouping`` no-ops on zero
    unassigned ops, so the installed LLM stub asserts it is never
    actually invoked.
    """
    tenant = uuid.uuid4()
    key, token = _token(tenant_id=tenant)

    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        spec_url = f"https://{_SPEC_HOST}/ghost.yaml"
        mock_router.get(spec_url).mock(
            return_value=httpx.Response(
                200,
                content=_ZERO_OP_SPEC.encode(),
                headers={"content-type": "application/yaml"},
            ),
        )

        ingest = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": _PRODUCT,
                "version": _VERSION,
                "impl_id": _IMPL_ID,
                "specs": [{"uri": spec_url}],
                "async": False,
            },
            headers=_authed(token),
        )
        assert ingest.status_code == 200, ingest.text
        ingestion = ingest.json()["ingestion"]
        assert ingestion["inserted_count"] == 0
        assert ingestion["connector_registered"] is True
        assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()

        # The stub is now visible in the catalog as a class-side row.
        before = client.get("/api/v1/connectors", headers=_authed(token))
        assert _CONNECTOR_ID in _connector_ids(before)

        # DELETE the stub — registry-only (no rows ever landed).
        deleted = client.delete(
            f"/api/v1/connectors/{_CONNECTOR_ID}",
            headers=_authed(token),
        )
        assert deleted.status_code == 204, deleted.text

        # Gone from the catalog, gone from the registry.
        after = client.get("/api/v1/connectors", headers=_authed(token))
        assert _CONNECTOR_ID not in _connector_ids(after)

    assert (_PRODUCT, _VERSION, _IMPL_ID) not in all_connectors_v2()
    assert await _row_counts(tenant) == (0, 0)
    assert await _row_counts(None) == (0, 0)
    assert await _service_delete_audit_count() == 1
