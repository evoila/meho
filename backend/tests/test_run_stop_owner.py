# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may stop a hosted agent run or a background operation run.

* The principal the run acts for may stop it. A scheduled run acts for the
  agent's own login, so that login may stop it.
* A person with the ``tenant_admin`` role, or a platform admin, may stop
  any run of the tenant.
* Everyone else gets 403 with a text that says who may stop the run, and
  the run goes on.

The REST tests go through the real token check, like
``tests/test_api_v1_agent_runs.py`` and ``tests/test_api_v1_operation_runs.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.agent.invocation import AgentInvoker, reset_agent_invoker_for_testing
from meho_backplane.api.v1.operation_runs import router as operation_runs_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentRun,
    AgentRunStatus,
    AgentRunTrigger,
    OperationRun,
    OperationRunOrigin,
    OperationRunStatus,
    Tenant,
)
from meho_backplane.main import app
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.operations.agent_run import create_run
from meho_backplane.operations.run_stop_rule import (
    MACHINE_LOGIN_REASON,
    PERSON_REASON,
    run_stop_refusal,
)
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
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_PERSON = "person-one"
_OTHER_PERSON = "person-two"
_AGENT = "agent-login-one"
_SERVICE = "service-login-one"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_agent_invoker_for_testing(None)


def _token(
    key: Any,
    *,
    sub: str,
    role: TenantRole = TenantRole.OPERATOR,
    kind: str | None = None,
    platform_admin: bool | None = None,
) -> str:
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=DEFAULT_TENANT_ID,
        principal_kind=kind,
        platform_admin=platform_admin,
    )


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def _operator(
    sub: str,
    *,
    role: TenantRole = TenantRole.OPERATOR,
    kind: PrincipalKind = PrincipalKind.USER,
    platform_admin: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="not-a-real-jwt",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        platform_admin=platform_admin,
    )


@pytest.mark.parametrize(
    ("operator", "owner", "expected"),
    [
        pytest.param(_operator(_PERSON), _PERSON, None, id="person-own-run"),
        pytest.param(_operator(_PERSON), _OTHER_PERSON, PERSON_REASON, id="operator-other-run"),
        pytest.param(
            _operator(_PERSON, role=TenantRole.TENANT_ADMIN),
            _OTHER_PERSON,
            None,
            id="tenant-admin-other-run",
        ),
        pytest.param(
            _operator(_PERSON, platform_admin=True), _OTHER_PERSON, None, id="platform-admin"
        ),
        pytest.param(_operator(_PERSON), None, PERSON_REASON, id="operator-run-without-owner"),
        pytest.param(_operator(_AGENT, kind=PrincipalKind.AGENT), _AGENT, None, id="agent-own-run"),
        pytest.param(
            _operator(_AGENT, kind=PrincipalKind.AGENT),
            _PERSON,
            MACHINE_LOGIN_REASON,
            id="agent-person-run",
        ),
        pytest.param(
            _operator(_SERVICE, role=TenantRole.TENANT_ADMIN, kind=PrincipalKind.SERVICE),
            _PERSON,
            MACHINE_LOGIN_REASON,
            id="admin-service-login-person-run",
        ),
    ],
)
def test_run_stop_rule(operator: Operator, owner: str | None, expected: str | None) -> None:
    assert run_stop_refusal(operator, owner) == expected


# ---------------------------------------------------------------------------
# Hosted agent runs: POST /api/v1/agents/runs/{handle}/cancel
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    reset_agent_invoker_for_testing(AgentInvoker())
    yield TestClient(app)


async def _seed_agent_run(
    owner: str, *, trigger: AgentRunTrigger = AgentRunTrigger.DIRECT
) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-stop", name="Tenant stop"))
        run = await create_run(
            session,
            tenant_id=_TENANT,
            identity_sub=owner,
            trigger=trigger,
            model_tier="standard",
        )
        run.status = AgentRunStatus.RUNNING.value
        await session.flush()
        run_id = run.id
        await session.commit()
    return run_id


async def _agent_run_status(run_id: uuid.UUID) -> str:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(AgentRun, run_id)
        assert row is not None
        return row.status


def _cancel_agent_run(client: TestClient, run_id: uuid.UUID, token_args: dict[str, Any]) -> Any:
    key = make_rsa_keypair("kid-run-stop")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.post(
            f"/api/v1/agents/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {_token(key, **token_args)}"},
        )


@pytest.mark.asyncio
async def test_an_agent_login_cannot_stop_a_persons_agent_run(agent_client: TestClient) -> None:
    run_id = await _seed_agent_run(_PERSON)
    resp = _cancel_agent_run(agent_client, run_id, {"sub": _AGENT, "kind": "agent"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"agent_run_cancel_forbidden: {MACHINE_LOGIN_REASON}"
    assert await _agent_run_status(run_id) == AgentRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_an_agent_login_stops_its_own_scheduled_run(agent_client: TestClient) -> None:
    run_id = await _seed_agent_run(_AGENT, trigger=AgentRunTrigger.SCHEDULED)
    resp = _cancel_agent_run(agent_client, run_id, {"sub": _AGENT, "kind": "agent"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_person_stops_their_own_agent_run(agent_client: TestClient) -> None:
    run_id = await _seed_agent_run(_PERSON)
    resp = _cancel_agent_run(agent_client, run_id, {"sub": _PERSON})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_an_operator_cannot_stop_another_persons_agent_run(
    agent_client: TestClient,
) -> None:
    run_id = await _seed_agent_run(_OTHER_PERSON)
    resp = _cancel_agent_run(agent_client, run_id, {"sub": _PERSON})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"agent_run_cancel_forbidden: {PERSON_REASON}"
    assert "tenant admin" in resp.json()["detail"]
    assert await _agent_run_status(run_id) == AgentRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_tenant_admin_stops_another_persons_agent_run(agent_client: TestClient) -> None:
    run_id = await _seed_agent_run(_OTHER_PERSON)
    resp = _cancel_agent_run(
        agent_client, run_id, {"sub": _PERSON, "role": TenantRole.TENANT_ADMIN}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Background operation runs: POST /api/v1/operations/runs/{handle}/cancel
# ---------------------------------------------------------------------------


def _operation_runs_app() -> FastAPI:
    built = FastAPI()
    built.add_middleware(AuditMiddleware)
    built.add_middleware(RequestContextMiddleware)
    built.include_router(operation_runs_router)
    return built


async def _seed_operation_run(owner: str) -> uuid.UUID:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=_TENANT,
            identity_sub=owner,
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="vmware-rest-9.0",
            op_id="vm.list",
            status=OperationRunStatus.RUNNING.value,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _operation_run(run_id: uuid.UUID) -> OperationRun:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(OperationRun, run_id)
        assert row is not None
        return row


def _cancel_operation_run(run_id: uuid.UUID, token_args: dict[str, Any]) -> Any:
    key = make_rsa_keypair("kid-op-run-stop")
    client = TestClient(_operation_runs_app())
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        return client.post(
            f"/api/v1/operations/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {_token(key, **token_args)}"},
        )


@pytest.mark.asyncio
async def test_a_service_login_cannot_stop_another_principals_operation_run() -> None:
    run_id = await _seed_operation_run(_PERSON)
    resp = _cancel_operation_run(run_id, {"sub": _SERVICE, "kind": "service"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"operation_run_cancel_forbidden: {MACHINE_LOGIN_REASON}"
    row = await _operation_run(run_id)
    assert row.status == OperationRunStatus.RUNNING.value
    assert row.ended_at is None


@pytest.mark.asyncio
async def test_a_service_login_stops_its_own_operation_run() -> None:
    run_id = await _seed_operation_run(_SERVICE)
    resp = _cancel_operation_run(run_id, {"sub": _SERVICE, "kind": "service"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_an_operator_cannot_stop_another_persons_operation_run() -> None:
    run_id = await _seed_operation_run(_OTHER_PERSON)
    resp = _cancel_operation_run(run_id, {"sub": _PERSON})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == f"operation_run_cancel_forbidden: {PERSON_REASON}"
    assert (await _operation_run(run_id)).status == OperationRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_a_tenant_admin_stops_another_persons_operation_run() -> None:
    run_id = await _seed_operation_run(_OTHER_PERSON)
    resp = _cancel_operation_run(run_id, {"sub": _PERSON, "role": TenantRole.TENANT_ADMIN})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_platform_admin_stops_another_persons_operation_run() -> None:
    run_id = await _seed_operation_run(_OTHER_PERSON)
    resp = _cancel_operation_run(run_id, {"sub": _PERSON, "platform_admin": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
