# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Route tests for async governed dispatch (#3079).

Covers the HTTP surface: the ``async: true`` branch on
``POST /api/v1/operations/call`` (202 + handle) and the
``/api/v1/operations/runs*`` poll / list / cancel routes, plus the
byte-identical sync path (criterion 4). The runs router is mounted
**before** the operations router so the literal ``/runs`` list route wins
over the operations router's ``/{descriptor_id}`` catch-all -- the same
ordering :mod:`meho_backplane.main` uses.

Tokens are minted through the real ``verify_jwt_and_bind`` chain via the
shared OIDC helpers, mirroring :mod:`tests.test_api_v1_operations`. The DB
rows are inserted directly (SQLite does not enforce the ``tenant_id`` FK in
the test path, so the pinned ``DEFAULT_TENANT_ID`` needs no tenant row).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import meho_backplane.api.v1.operations as operations_module
from meho_backplane.api.v1.operation_runs import router as operation_runs_router
from meho_backplane.api.v1.operations import router as operations_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationRun, OperationRunOrigin, OperationRunStatus
from meho_backplane.middleware import RequestContextMiddleware
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
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_jwks_cache() -> Iterator[None]:
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    # Runs router first -- mirrors main.py so /runs is not shadowed by the
    # operations router's /{descriptor_id} catch-all.
    app.include_router(operation_runs_router)
    app.include_router(operations_router)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(_build_app())


def _token(key: Any, *, role: TenantRole = TenantRole.OPERATOR) -> str:
    return mint_token(key, sub="op-1", tenant_role=role.value, tenant_id=DEFAULT_TENANT_ID)


async def _insert_run(
    *,
    status: OperationRunStatus = OperationRunStatus.PENDING,
    result: dict[str, Any] | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
    op_id: str = "vm.list",
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            identity_sub="op-1",
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="vmware-rest-9.0",
            op_id=op_id,
            status=status.value,
            result=result,
        )
        session.add(run)
        await session.commit()
        return run.id


class _FakeService:
    """Stand-in for the run service: records submits, returns a fixed handle."""

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.submitted: list[dict[str, Any]] = []

    async def submit_call(self, operator: Any, arguments: dict[str, Any]) -> uuid.UUID:
        self.submitted.append(arguments)
        return self.run_id


# ---------------------------------------------------------------------------
# POST /call async branch + sync unchanged
# ---------------------------------------------------------------------------


def test_call_async_returns_202_and_run_handle(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``async: true`` returns 202 + the durable run handle (does not dispatch inline)."""
    fixed = uuid.uuid4()
    fake = _FakeService(fixed)
    monkeypatch.setattr(operations_module, "get_operation_run_service", lambda: fake)

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            headers={"Authorization": f"Bearer {_token(key)}"},
            json={
                "connector_id": "vmware-rest-9.0",
                "op_id": "vm.list",
                "target": "dc-vcenter",
                "params": {"folder": "prod"},
                "async": True,
            },
        )
    assert response.status_code == 202
    body = response.json()
    assert body == {"run_id": str(fixed), "status": "pending", "async": True}
    # The async control is stripped before the dispatch arguments.
    assert fake.submitted and "async_" not in fake.submitted[0]
    assert fake.submitted[0]["op_id"] == "vm.list"


def test_call_sync_is_unchanged_and_does_not_submit_a_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``async`` the route dispatches inline (200) and creates no run."""
    fixed = uuid.uuid4()
    fake = _FakeService(fixed)
    monkeypatch.setattr(operations_module, "get_operation_run_service", lambda: fake)

    envelope = {"status": "ok", "op_id": "vm.list", "result": {"vms": []}, "duration_ms": 5.0}

    async def fake_call(operator: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        return envelope

    monkeypatch.setattr(operations_module, "call_operation", fake_call)

    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            "/api/v1/operations/call",
            headers={"Authorization": f"Bearer {_token(key)}"},
            json={"connector_id": "vmware-rest-9.0", "op_id": "vm.list", "target": "dc-vcenter"},
        )
    assert response.status_code == 200
    assert response.json() == envelope
    assert fake.submitted == []  # sync path never touches the run substrate


# ---------------------------------------------------------------------------
# GET /runs/{handle} poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_returns_persisted_result_envelope() -> None:
    """A completed run's full envelope is retrievable via the handle (#3079 core)."""
    envelope = {"status": "ok", "op_id": "vm.list", "result": {"vms": ["a"]}, "duration_ms": 8.0}
    run_id = await _insert_run(status=OperationRunStatus.SUCCEEDED, result=envelope)

    key = make_rsa_keypair("kid-A")
    client = TestClient(_build_app())
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/runs/{run_id}",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["status"] == "succeeded"
    assert body["result"] == envelope
    assert body["connector_id"] == "vmware-rest-9.0"


def test_poll_unknown_handle_is_404(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            f"/api/v1/operations/runs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "operation_run_not_found"


# ---------------------------------------------------------------------------
# GET /runs list  +  POST /runs/{handle}/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_is_tenant_scoped_and_not_shadowed_by_descriptor_route() -> None:
    """``GET /runs`` lists this tenant's runs (and is not caught by /{descriptor_id})."""
    await _insert_run(op_id="vm.list")
    await _insert_run(op_id="vm.power")
    # A different tenant's run must not appear.
    await _insert_run(tenant_id="00000000-0000-0000-0000-0000000000ff", op_id="other")

    key = make_rsa_keypair("kid-A")
    client = TestClient(_build_app())
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.get(
            "/api/v1/operations/runs",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    assert {r["op_id"] for r in rows} == {"vm.list", "vm.power"}


@pytest.mark.asyncio
async def test_cancel_pending_run_returns_cancelled() -> None:
    run_id = await _insert_run(status=OperationRunStatus.PENDING)

    key = make_rsa_keypair("kid-A")
    client = TestClient(_build_app())
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/operations/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_terminal_run_is_409() -> None:
    run_id = await _insert_run(status=OperationRunStatus.SUCCEEDED, result={"status": "ok"})

    key = make_rsa_keypair("kid-A")
    client = TestClient(_build_app())
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/operations/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "operation_run_not_cancellable"


def test_cancel_unknown_run_is_404(client: TestClient) -> None:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = client.post(
            f"/api/v1/operations/runs/{uuid.uuid4()}/cancel",
            headers={"Authorization": f"Bearer {_token(key)}"},
        )
    assert response.status_code == 404
    assert response.json()["detail"] == "operation_run_not_found"
