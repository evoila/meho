# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for :mod:`meho_backplane.api.v1.operations`.

Coverage matrix (G0.6-T8 / Task #399):

* ``GET /api/v1/operations/groups`` returns the same payload shape as the
  meta-tool; unauthenticated -> 401.
* ``GET /api/v1/operations/search`` rejects ``limit > 50`` at the
  Pydantic Query layer.
* ``POST /api/v1/operations/call`` returns the dispatcher's
  OperationResult envelope on the response body; a malformed target
  surfaces as 400 (caught from the meta-tool's ValueError).
* ``GET /api/v1/operations/{descriptor_id}`` is gated on
  ``tenant_admin``; an OPERATOR-role token returns 403. A descriptor
  that doesn't exist returns 404.

The route tests use the shared OIDC helpers (``_oidc_jwt_helpers``) to
mint Bearer tokens through the real ``verify_jwt_and_bind`` chain --
same shape ``test_api_v1_targets`` does. The chassis settings
(KEYCLOAK_*, VAULT_*) come from the per-file ``_settings_env`` fixture.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._oidc_jwt_helpers import ISSUER as _ISSUER


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
    clear_jwks_cache()
    yield
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _empty_connector_registry() -> Iterator[None]:
    clear_registry()
    reset_dispatcher_caches()
    yield
    clear_registry()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    def _fake() -> AsyncMock:
        return service

    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        _fake,
    )
    return service


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(operations_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _operator_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(key, sub="op-1", tenant_role=TenantRole.OPERATOR.value, tenant_id=tenant_id)


def _admin_token(key: Any, tenant_id: str = DEFAULT_TENANT_ID) -> str:
    return mint_token(
        key,
        sub="admin-1",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=tenant_id,
    )


async def _seed_group(
    *,
    product: str = "vault",
    version: str = "1.x",
    impl_id: str = "vault",
    group_key: str = "kv",
    name: str = "KV",
    when_to_use: str = "use this.",
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    group_id = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            OperationGroup(
                id=group_id,
                tenant_id=None,
                product=product,
                version=version,
                impl_id=impl_id,
                group_key=group_key,
                name=name,
                when_to_use=when_to_use,
                review_status="enabled",
            )
        )
    return group_id


async def _seed_descriptor(
    *,
    op_id: str,
    summary: str = "Read.",
    description: str = "reads a secret.",
    tenant_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    llm_instructions: dict[str, Any] | None = None,
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    descriptor_id = uuid.uuid4()
    async with sessionmaker() as s, s.begin():
        s.add(
            EndpointDescriptor(
                id=descriptor_id,
                tenant_id=tenant_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=op_id,
                source_kind="typed",
                method=None,
                path=None,
                handler_ref="tests.test_api_v1_operations._noop_handler",
                summary=summary,
                description=description,
                group_id=group_id,
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=llm_instructions,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=None,
                custom_description=None,
                custom_notes=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return descriptor_id


async def _noop_handler(
    operator: Any,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Stand-in handler used in dispatcher round-trips from the route tests."""
    return {"echo": params}


# ---------------------------------------------------------------------------
# GET /api/v1/operations/groups
# ---------------------------------------------------------------------------


def test_get_groups_requires_authentication(client: TestClient) -> None:
    """No Bearer header -> 401."""
    response = client.get("/api/v1/operations/groups?connector_id=vault-1.x")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_groups_returns_meta_tool_payload(
    client: TestClient,
    stub_embedding_service: AsyncMock,
) -> None:
    """The route returns the same shape :func:`list_operation_groups` produces."""
    await _seed_group(group_key="kv", name="KV v2", when_to_use="use for kv.")

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=vault-1.x",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["connector_id"] == "vault-1.x"
    assert any(g["group_key"] == "kv" for g in body["groups"])


def test_get_groups_unknown_connector_returns_empty_list(client: TestClient) -> None:
    """Unknown connector -> 200 with empty groups (not 404)."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/groups?connector_id=ghost-9.9",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json() == {"connector_id": "ghost-9.9", "groups": []}


# ---------------------------------------------------------------------------
# GET /api/v1/operations/search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_search_returns_hits(
    client: TestClient,
    stub_embedding_service: AsyncMock,
) -> None:
    """A query against a seeded descriptor returns a ranked hit."""
    await _seed_descriptor(op_id="vault.kv.read", summary="Read a secret.")
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&query=read",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["hits"]) == 1
    assert body["hits"][0]["op_id"] == "vault.kv.read"
    assert "query_duration_ms" in body


def test_get_search_rejects_limit_over_50(client: TestClient) -> None:
    """``limit=51`` violates the Pydantic Query ``le=50`` constraint -> 422."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/search?connector_id=vault-1.x&query=read&limit=51",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/v1/operations/call
# ---------------------------------------------------------------------------


def test_post_call_missing_target_name_returns_400(client: TestClient) -> None:
    """``target={}`` (no name) surfaces as 400 from the meta-tool's ValueError."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            json={
                "connector_id": "vault-1.x",
                "op_id": "vault.kv.read",
                "target": {},
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 400


def test_post_call_unknown_op_returns_200_with_error_envelope(client: TestClient) -> None:
    """The dispatcher's structured-error envelope rides on a 200 body."""
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            json={
                "connector_id": "vault-1.x",
                "op_id": "vault.does.not.exist",
                "target": None,
                "params": {},
            },
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["error"].startswith("unknown_op:")


# ---------------------------------------------------------------------------
# GET /api/v1/operations/{descriptor_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_descriptor_returns_full_row_for_admin(
    client: TestClient,
) -> None:
    """An admin token sees the full descriptor incl. ``llm_instructions``."""
    descriptor_id = await _seed_descriptor(
        op_id="vault.kv.read",
        llm_instructions={"when_to_call": "use after search."},
    )
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/{descriptor_id}",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["op_id"] == "vault.kv.read"
    assert body["llm_instructions"] == {"when_to_call": "use after search."}


def test_get_descriptor_requires_admin_role(client: TestClient) -> None:
    """An operator-role token gets 403 on the descriptor inspection route."""
    descriptor_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/{descriptor_id}",
            headers={"Authorization": f"Bearer {_operator_token(key)}"},
        )
    assert response.status_code == 403


def test_get_descriptor_unknown_id_returns_404(client: TestClient) -> None:
    """A descriptor id that doesn't exist returns 404."""
    descriptor_id = uuid.uuid4()
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/{descriptor_id}",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 404
