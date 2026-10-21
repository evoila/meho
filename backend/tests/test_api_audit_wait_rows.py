# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The REST read side of a run whose step waits for approval (#4275).

The run's own ``awaiting_approval`` row (``test_operations_composite_wait_audit.py``)
is what makes two reads work that answered wrong before:

* ``GET /api/v1/audit/{id}/trace`` for the run's audit id answers 200 with
  ``trace_present=true`` when capture is on, not 404;
* ``POST /api/v1/audit/query`` with ``result_status="pending"`` and
  ``event_class="operation_execution"`` returns the run's row and not the
  step's ``approval.request`` row.

The rows are written by the real dispatcher; only the read goes through the
FastAPI app with a mocked identity provider.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.api.v1.audit import router as audit_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.settings import get_settings

from . import _composite_wait_audit_helpers as h
from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env :class:`Settings` reads; reset the JWKS and flight-recorder caches."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    clear_jwks_cache()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(audit_router)
    yield TestClient(app)


async def _seed_tenant(*, capture: bool) -> UUID:
    """A tenant row; ``capture`` turns the flight recorder on for it."""
    tenant_id = uuid.uuid4()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        s.add(
            Tenant(
                id=tenant_id,
                slug=f"wait-rows-{tenant_id.hex[:8]}",
                name="Wait rows",
                flight_recorder_enabled=capture,
            )
        )
        await s.commit()
    return tenant_id


def _headers(tenant_id: UUID) -> tuple[Any, dict[str, str]]:
    key = make_rsa_keypair("kid-4275")
    token = mint_token(
        key, sub="op-reader", tenant_role=TenantRole.OPERATOR.value, tenant_id=str(tenant_id)
    )
    return key, {"Authorization": f"Bearer {token}"}


async def _waiting_run(session: AsyncSession, embedding: AsyncMock, tenant_id: UUID) -> Any:
    """Run the composite whose step waits; return the dispatcher's result."""
    await h.register_child(embedding)
    await h.register_composite(session, h.WAIT_OP_ID, "step_waits_composite")
    result = await h.run(h.WAIT_OP_ID, h.operator(tenant_id=tenant_id))
    assert result.status == "awaiting_approval", result.error
    assert result.audit_id is not None
    return result


async def test_trace_route_finds_the_waiting_runs_row(
    client: TestClient, session: AsyncSession, stub_embedding_service: AsyncMock
) -> None:
    """With capture on, the run's audit id answers 200 with its trace, not 404."""
    tenant_id = await _seed_tenant(capture=True)
    result = await _waiting_run(session, stub_embedding_service, tenant_id)

    key, headers = _headers(tenant_id)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = client.get(f"/api/v1/audit/{result.audit_id}/trace", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["audit_id"] == str(result.audit_id)
    assert body["event_class"] == "operation_execution"
    assert body["trace_present"] is True
    assert body["trace"]["spans"]


async def test_query_pending_execution_rows_returns_the_run_not_the_request(
    client: TestClient, session: AsyncSession, stub_embedding_service: AsyncMock
) -> None:
    """``result_status=pending`` + ``event_class=operation_execution`` → the run's row only."""
    tenant_id = await _seed_tenant(capture=False)
    result = await _waiting_run(session, stub_embedding_service, tenant_id)
    request = await h.request(UUID(result.extras["approval_request_id"]))

    key, headers = _headers(tenant_id)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        both = client.post(
            "/api/v1/audit/query", json={"result_status": "pending"}, headers=headers
        )
        runs_only = client.post(
            "/api/v1/audit/query",
            json={"result_status": "pending", "event_class": "operation_execution"},
            headers=headers,
        )

    assert both.status_code == 200, both.text
    assert sorted(row["id"] for row in both.json()["rows"]) == sorted(
        [str(result.audit_id), str(request.request_audit_id)]
    )
    assert runs_only.status_code == 200, runs_only.text
    rows = runs_only.json()["rows"]
    assert [row["id"] for row in rows] == [str(result.audit_id)]
    assert rows[0]["op_id"] == h.WAIT_OP_ID
    assert rows[0]["result_status"] == "pending"
    assert rows[0]["event_class"] == "operation_execution"
