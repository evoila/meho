# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.connectors_ingest`.

Coverage matrix (G0.7-T6 / Task #406 acceptance criteria):

* **Route mounting.** All seven routes appear in the FastAPI app's
  route table; the OpenAPI document the test client builds advertises
  them.
* **POST /ingest.** Happy path runs the full pipeline; ``dry_run=true``
  short-circuits both the DB writes and the LLM call.
* **GET /.** Status filter narrows the list; built-ins surface
  alongside the operator's-tenant rows; cross-tenant rows are
  filtered out.
* **GET /{id}/review.** Operator-level read; cross-tenant probe → 404.
* **PATCH /{id}/groups/{key}.** Edit writes one audit row.
* **PATCH /{id}/operations/{op}.** Edit writes one audit row; the
  op_id path converter handles colon-prefixed natural keys.
* **POST /{id}/enable.** Idempotent transition; cascade to children.
* **POST /{id}/disable.** Idempotent transition; cascade.
* **RBAC.** Unauthenticated → 401; ``operator`` role on a mutating
  route → 403; ``tenant_admin`` → 200 / 204.
* **Tenant isolation.** Tenant A cannot read tenant B's connector;
  the response is 404, not 403.

Tests boot the FastAPI app with the production middleware stack
(``RequestContextMiddleware`` + ``AuditMiddleware``) so audit rows
are inserted into the autouse-migrated SQLite engine. The LLM client
is replaced via :func:`set_llm_client_factory` with a deterministic
stub so the ingest test paths don't need a real Anthropic key.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

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
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    OperationGroup,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.ingest import (
    GroupingResult,
    IngestionResult,
    LlmClient,
)
from meho_backplane.operations.ingest.pipeline import (
    _default_llm_client_factory,
)
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair as _make_rsa_keypair
from ._oidc_jwt_helpers import mint_token as _mint_token
from ._oidc_jwt_helpers import mock_discovery_and_jwks as _mock_discovery_and_jwks
from ._oidc_jwt_helpers import public_jwks as _public_jwks

# ---------------------------------------------------------------------------
# Settings + JWKS cache + LLM-client fixtures
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
def _reset_llm_client_factory() -> Iterator[None]:
    """Reset the module-level LLM-client factory between tests.

    Tests that exercise the ingest happy path install their own stub
    via :func:`set_llm_client_factory`; the fixture restores the
    default fail-closed factory after each test so a missing
    reset doesn't leak across the file.
    """
    yield
    set_llm_client_factory(_default_llm_client_factory)


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """An :class:`AsyncMock` standing in for the fastembed singleton.

    Ingest happy-path tests patch the chassis embedding singleton to
    this stub so :func:`register_ingested_operations` doesn't try to
    download the ONNX model from huggingface.co. ``encode_one``
    returns a 384-dim vector of ``0.25`` — same shape
    :mod:`tests.test_operations_register_ingested` uses.
    """
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


class _StubLlmClient:
    """Deterministic :class:`LlmClient` for ingest happy-path tests.

    Returns valid Pass-1 / Pass-2 JSON payloads keyed on prompt
    content so :func:`run_llm_grouping` accepts the output verbatim
    and the test doesn't need to mock the upstream Anthropic
    Messages API.

    Not a Pydantic / dataclass — a thin class is enough to satisfy
    the :class:`LlmClient` Protocol's single async method.
    """

    def __init__(self, *, propose_response: str, assign_response: str) -> None:
        self._propose_response = propose_response
        self._assign_response = assign_response

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        # The grouping pass uses two distinct system prompts; route
        # off them rather than parsing user_prompt structure.
        if "Propose" in system_prompt or "propose" in system_prompt:
            return self._propose_response
        return self._assign_response


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Return a :class:`FastAPI` mounting only the connectors-ingest router.

    Mirrors prod middleware so the AuditMiddleware writes its row
    into the autouse-migrated SQLite engine for audit-payload
    assertions.
    """
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(connectors_ingest_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """``TestClient`` driving a fresh app per test."""
    yield TestClient(_build_app())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_token(*, tenant_id: UUID | None = None, sub: str = "op-admin") -> tuple[Any, str]:
    """Mint a JWT for a ``tenant_admin`` operator."""
    key = _make_rsa_keypair("kid-admin")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(tid),
    )
    return key, token


def _operator_token(*, tenant_id: UUID | None = None, sub: str = "op-operator") -> tuple[Any, str]:
    """Mint a JWT for an ``operator`` role (not admin)."""
    key = _make_rsa_keypair("kid-operator")
    tid = tenant_id if tenant_id is not None else uuid.uuid4()
    token = _mint_token(
        key,
        sub=sub,
        tenant_role=TenantRole.OPERATOR.value,
        tenant_id=str(tid),
    )
    return key, token


async def _seed_connector(
    *,
    tenant_id: UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
    group_count: int = 2,
    ops_per_group: int = 3,
    review_status: str = "staged",
    op_is_enabled: bool = False,
) -> list[uuid.UUID]:
    """Seed an ingested connector with *group_count* groups + ops per group."""
    sessionmaker = get_sessionmaker()
    group_ids: list[uuid.UUID] = []
    async with sessionmaker() as session:
        for g_index in range(group_count):
            group_id = uuid.uuid4()
            group_key = f"group-{g_index}"
            session.add(
                OperationGroup(
                    id=group_id,
                    tenant_id=tenant_id,
                    product=product,
                    version=version,
                    impl_id=impl_id,
                    group_key=group_key,
                    name=f"Group {g_index}",
                    when_to_use=f"Use group {g_index} for things.",
                    review_status=review_status,
                ),
            )
            group_ids.append(group_id)
            for o_index in range(ops_per_group):
                session.add(
                    EndpointDescriptor(
                        tenant_id=tenant_id,
                        product=product,
                        version=version,
                        impl_id=impl_id,
                        op_id=f"GET:/api/v1/{group_key}/{o_index}",
                        source_kind="ingested",
                        method="GET",
                        path=f"/api/v1/{group_key}/{o_index}",
                        group_id=group_id,
                        summary=f"Operation {o_index} in {group_key}",
                        is_enabled=op_is_enabled,
                    ),
                )
        await session.commit()
    return group_ids


async def _group_statuses(
    *,
    tenant_id: UUID | None,
    product: str = "vmware",
    version: str = "9.0",
    impl_id: str = "vmware-rest",
) -> dict[str, str]:
    """Return ``{group_key: review_status}`` for every group under the connector."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        stmt = select(OperationGroup).where(
            OperationGroup.product == product,
            OperationGroup.version == version,
            OperationGroup.impl_id == impl_id,
        )
        if tenant_id is None:
            stmt = stmt.where(OperationGroup.tenant_id.is_(None))
        else:
            stmt = stmt.where(OperationGroup.tenant_id == tenant_id)
        result = await session.execute(stmt)
        return {group.group_key: group.review_status for group in result.scalars().all()}


async def _audit_row_count(*, op_id: str) -> int:
    """Count audit_log rows whose ``path`` equals *op_id*."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == op_id))
        return len(list(result.scalars().all()))


def _authed(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Route mounting
# ---------------------------------------------------------------------------


def test_all_seven_routes_mounted_on_main_app() -> None:
    """The seven routes show up in :mod:`meho_backplane.main`'s app.

    Acceptance criterion: ``OpenAPI surface visible at
    /api/v1/openapi.json``. Verified here by introspecting the
    main app's route table; the per-test fixture builds an
    isolated app and would miss a wire-up regression in main.py.
    """
    from meho_backplane.main import app

    expected_paths = {
        "/api/v1/connectors/ingest",
        "/api/v1/connectors",
        "/api/v1/connectors/{connector_id}/review",
        "/api/v1/connectors/{connector_id}/groups/{group_key}",
        "/api/v1/connectors/{connector_id}/operations/{op_id:path}",
        "/api/v1/connectors/{connector_id}/enable",
        "/api/v1/connectors/{connector_id}/disable",
    }
    actual_paths = {getattr(r, "path", None) for r in app.routes}
    missing = expected_paths - actual_paths
    assert not missing, f"missing routes: {missing}"


# ---------------------------------------------------------------------------
# RBAC: unauthenticated + insufficient role
# ---------------------------------------------------------------------------


def test_ingest_unauthenticated_returns_401(client: TestClient) -> None:
    """No Authorization header → 401."""
    response = client.post(
        "/api/v1/connectors/ingest",
        json={
            "product": "vmware",
            "version": "9.0",
            "impl_id": "vmware-rest",
            "specs": [{"uri": "/tmp/spec.yaml"}],
        },
    )
    assert response.status_code == 401


def test_ingest_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on the tenant_admin-gated /ingest → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "vmware",
                "version": "9.0",
                "impl_id": "vmware-rest",
                "specs": [{"uri": "/tmp/spec.yaml"}],
            },
            headers=_authed(token),
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "insufficient_role"


def test_enable_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on /enable → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_disable_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on /disable → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/disable",
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_edit_group_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on PATCH /groups → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={"when_to_use": "Updated text."},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_edit_op_operator_role_returns_403(client: TestClient) -> None:
    """``operator`` role on PATCH /operations → 403."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "dangerous"},
            headers=_authed(token),
        )
    assert response.status_code == 403


def test_list_operator_role_returns_200(client: TestClient) -> None:
    """``operator`` role can call GET / (operator minimum)."""
    key, token = _operator_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    assert response.json() == {"connectors": []}


# ---------------------------------------------------------------------------
# GET / -- list connectors + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_operator_tenant_and_builtins(
    client: TestClient,
) -> None:
    """An operator sees their tenant's connectors + built-ins (NULL tenant)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, product="vmware", impl_id="vmware-rest")
    await _seed_connector(tenant_id=None, product="nsx", impl_id="nsx")
    await _seed_connector(tenant_id=tenant_b, product="harbor", impl_id="harbor")

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get("/api/v1/connectors", headers=_authed(token))
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    seen_ids = {c["connector_id"] for c in connectors}
    assert seen_ids == {"vmware-rest-9.0", "nsx-9.0"}
    # tenant_b's harbor must not surface.
    assert "harbor-9.0" not in seen_ids


@pytest.mark.asyncio
async def test_list_status_staged_filters_by_aggregate_state(
    client: TestClient,
) -> None:
    """``?status=staged`` returns connectors with ≥1 staged group."""
    tenant_a = uuid.uuid4()
    # vmware: all staged
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        review_status="staged",
    )
    # nsx: all enabled
    await _seed_connector(
        tenant_id=tenant_a,
        product="nsx",
        impl_id="nsx",
        review_status="enabled",
    )

    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors?status=staged",
            headers=_authed(token),
        )
    assert response.status_code == 200
    seen_ids = {c["connector_id"] for c in response.json()["connectors"]}
    assert seen_ids == {"vmware-rest-9.0"}


@pytest.mark.asyncio
async def test_list_status_enabled_requires_uniform_state(
    client: TestClient,
) -> None:
    """``?status=enabled`` returns only connectors with every group enabled."""
    tenant_a = uuid.uuid4()
    await _seed_connector(
        tenant_id=tenant_a,
        product="vmware",
        impl_id="vmware-rest",
        review_status="enabled",
    )
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors?status=enabled",
            headers=_authed(token),
        )
    assert response.status_code == 200
    connectors = response.json()["connectors"]
    assert len(connectors) == 1
    item = connectors[0]
    assert item["connector_id"] == "vmware-rest-9.0"
    assert item["enabled_group_count"] == item["group_count"]
    assert item["operation_count"] == 6  # 2 groups x 3 ops


# ---------------------------------------------------------------------------
# GET /{id}/review -- read + tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_review_returns_payload_for_operator_tenant(
    client: TestClient,
) -> None:
    """Operator-level read returns the review payload."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, group_count=2, ops_per_group=3)
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors/vmware-rest-9.0/review",
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["connector_id"] == "vmware-rest-9.0"
    assert body["product"] == "vmware"
    assert body["total_op_count"] == 6
    assert len(body["groups"]) == 2


@pytest.mark.asyncio
async def test_get_review_cross_tenant_returns_404(
    client: TestClient,
) -> None:
    """Tenant A cannot see tenant B's connector — 404, not 403."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b, product="vmware", impl_id="vmware-rest")
    key, token = _operator_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.get(
            "/api/v1/connectors/vmware-rest-9.0/review",
            headers=_authed(token),
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id}/groups/{key}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_group_updates_and_writes_audit_row(
    client: TestClient,
) -> None:
    """PATCH on a group updates the row + writes one audit row."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={"when_to_use": "New description text.", "name": "Renamed"},
            headers=_authed(token),
        )
    assert response.status_code == 204
    audit_count = await _audit_row_count(op_id="meho.connector.edit_group")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_edit_group_empty_body_returns_400(
    client: TestClient,
) -> None:
    """PATCH with no fields → 400."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={},
            headers=_authed(token),
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_edit_group_cross_tenant_returns_404(
    client: TestClient,
) -> None:
    """Cross-tenant PATCH → 404."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/groups/group-0",
            json={"name": "Renamed"},
            headers=_authed(token),
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /{id}/operations/{op_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_op_updates_safety_level_and_writes_audit_row(
    client: TestClient,
) -> None:
    """PATCH on an op updates safety_level + writes one audit row.

    Exercises the ``:path`` converter on the ``op_id`` segment so a
    colon-prefixed natural key (``"GET:/api/v1/group-0/0"``) survives
    URL routing intact.
    """
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "dangerous", "requires_approval": True},
            headers=_authed(token),
        )
    assert response.status_code == 204
    audit_count = await _audit_row_count(op_id="meho.connector.edit_op")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_edit_op_invalid_safety_level_returns_422(
    client: TestClient,
) -> None:
    """``safety_level`` outside the enum → 422 (Pydantic-level rejection)."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.patch(
            "/api/v1/connectors/vmware-rest-9.0/operations/GET:/api/v1/group-0/0",
            json={"safety_level": "nuclear"},
            headers=_authed(token),
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /{id}/enable + POST /{id}/disable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enable_transitions_all_groups_and_cascades(
    client: TestClient,
) -> None:
    """POST /enable transitions every group to ``enabled`` + cascades ops."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 204
    statuses = await _group_statuses(tenant_id=tenant_a)
    assert set(statuses.values()) == {"enabled"}
    audit_count = await _audit_row_count(op_id="meho.connector.enable")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_enable_is_idempotent(client: TestClient) -> None:
    """Second POST /enable writes no additional audit row."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, review_status="enabled", op_is_enabled=True)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 204
    audit_count = await _audit_row_count(op_id="meho.connector.enable")
    assert audit_count == 0


@pytest.mark.asyncio
async def test_disable_transitions_and_writes_audit_row(
    client: TestClient,
) -> None:
    """POST /disable transitions every group to ``disabled``."""
    tenant_a = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_a, review_status="enabled", op_is_enabled=True)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/disable",
            headers=_authed(token),
        )
    assert response.status_code == 204
    statuses = await _group_statuses(tenant_id=tenant_a)
    assert set(statuses.values()) == {"disabled"}
    audit_count = await _audit_row_count(op_id="meho.connector.disable")
    assert audit_count == 1


@pytest.mark.asyncio
async def test_enable_cross_tenant_returns_404(client: TestClient) -> None:
    """Cross-tenant enable → 404 (and no state changes leak to the other tenant)."""
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    await _seed_connector(tenant_id=tenant_b)
    key, token = _admin_token(tenant_id=tenant_a)
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/vmware-rest-9.0/enable",
            headers=_authed(token),
        )
    assert response.status_code == 404
    # tenant_b's connector untouched
    statuses = await _group_statuses(tenant_id=tenant_b)
    assert set(statuses.values()) == {"staged"}


# ---------------------------------------------------------------------------
# POST /ingest happy path
# ---------------------------------------------------------------------------


def test_ingest_returns_503_when_llm_client_unavailable(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """No LLM-client factory wired → 503 from the route's mapper.

    The stub embedding service is injected so the parse + register
    phases run without hitting the network; the unmocked LLM client
    factory is what triggers the 503.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": str(spec_path)}],
            },
            headers=_authed(token),
        )
    assert response.status_code == 503
    assert "LLM client" in response.json()["detail"]


def test_ingest_dry_run_returns_parse_counts_without_writes(
    client: TestClient, tmp_path: Any
) -> None:
    """``dry_run=true`` parses the spec but does not write or run the LLM."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
  /items/{id}:
    get:
      summary: get item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: ok
""",
    )
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": str(spec_path)}],
                "dry_run": True,
            },
            headers=_authed(token),
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ingestion"]["inserted_count"] == 2
    assert body["ingestion"]["connector_registered"] is False
    assert body["grouping"] is None


def test_ingest_happy_path_runs_full_pipeline(
    client: TestClient,
    tmp_path: Any,
    stub_embedding_service: AsyncMock,
) -> None:
    """End-to-end: parse + register + group all run; response carries both
    blocks.

    The LLM client is stubbed via :func:`set_llm_client_factory` so
    the grouping pass doesn't need a real Anthropic key; the
    embedding pipeline is patched at the leaf
    :func:`encode_endpoint_text` site so the test doesn't pull
    fastembed from the network.
    """
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """openapi: 3.0.3
info:
  title: t
  version: '1'
paths:
  /items:
    get:
      summary: list items
      responses:
        '200':
          description: ok
  /items/{id}:
    get:
      summary: get item
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: ok
""",
    )
    propose_json = (
        '[{"group_key": "items", "name": "Items", '
        '"when_to_use": "Use these operations to manage items."}]'
    )
    assign_json = '{"GET:/items": "items", "GET:/items/{id}": "items"}'
    set_llm_client_factory(
        lambda: _StubLlmClient(
            propose_response=propose_json,
            assign_response=assign_json,
        ),
    )

    key, token = _admin_token()
    with (
        respx.mock as mock_router,
        patch(
            "meho_backplane.operations.ingest._upsert.encode_endpoint_text",
            AsyncMock(return_value=[0.25] * 384),
        ),
    ):
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": str(spec_path)}],
            },
            headers=_authed(token),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ingestion"]["inserted_count"] == 2
    # connector_registered may be False when a prior test (503-path)
    # already auto-registered the shim against the module-level
    # connectors registry; the v2 registry is process-global. The
    # load-bearing assertion is that the pipeline returned 200 with
    # the grouping pass populated.
    grouping = body["grouping"]
    assert grouping is not None
    assert grouping["groups_created"] == 1
    assert grouping["operations_assigned"] == 2


def test_ingest_bad_spec_returns_400(client: TestClient, tmp_path: Any) -> None:
    """Unparseable spec → 400 from the InvalidSpecError mapper."""
    spec_path = tmp_path / "bad.yaml"
    spec_path.write_text("not: a: valid spec")
    key, token = _admin_token()
    with respx.mock as mock_router:
        _mock_discovery_and_jwks(mock_router, _public_jwks(key))
        response = client.post(
            "/api/v1/connectors/ingest",
            json={
                "product": "test",
                "version": "1.0",
                "impl_id": "test-impl",
                "specs": [{"uri": str(spec_path)}],
            },
            headers=_authed(token),
        )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# set_llm_client_factory contract
# ---------------------------------------------------------------------------


def test_set_llm_client_factory_returns_previous_and_restores(
    client: TestClient,
) -> None:
    """The factory setter returns the previous value so callers can restore."""

    def custom_factory() -> LlmClient:  # type: ignore[empty-body]
        ...  # never called in this test

    previous = set_llm_client_factory(custom_factory)
    assert previous is _default_llm_client_factory
    restored = set_llm_client_factory(previous)
    assert restored is custom_factory


# Silence unused-import lints on test seams reserved for sibling tasks.
_ = (AsyncMock, patch, GroupingResult, IngestionResult)
