# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who may stop a hosted agent run or a background operation run.

The rule (:func:`meho_backplane.auth.operator.may_stop_run`):

* The owner of the run (its ``identity_sub``) may stop it. A run that a
  login's own schedule started carries that login's ``sub``.
* A person with the ``tenant_admin`` role may stop any run of the tenant.
* Anyone else gets 403 with a text that says who may stop the run, and the
  run goes on. An agent or service login stops only its own runs, whatever
  its role.

Both REST routes are covered: ``POST /api/v1/agents/runs/{handle}/cancel``
and ``POST /api/v1/operations/runs/{handle}/cancel``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from meho_backplane.agent.invocation import AgentInvoker, reset_agent_invoker_for_testing
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import RUN_STOP_RULE, Operator, TenantRole, may_stop_run
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
from meho_backplane.operations.agent_run import create_run
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_PERSON = "person-owner"
_RESULT = {"status": "ok", "op_id": "vm.list", "data": [{"name": "vm-1"}]}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWKS_CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
    reset_agent_invoker_for_testing(
        AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: model))
    )
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_agent_invoker_for_testing(None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


#: The callers of the matrix: (sub, principal_kind or None for a person, role).
_CALLERS: dict[str, tuple[str, str | None, TenantRole]] = {
    "owner person": (_PERSON, None, TenantRole.OPERATOR),
    "other person, operator": ("person-other", None, TenantRole.OPERATOR),
    "other person, tenant admin": ("person-admin", None, TenantRole.TENANT_ADMIN),
    "agent login": ("agent-login", "agent", TenantRole.OPERATOR),
    "service login": ("service-login", "service", TenantRole.OPERATOR),
    "service login, tenant admin": ("service-admin", "service", TenantRole.TENANT_ADMIN),
}


def _token(key: Any, caller: str) -> str:
    sub, kind, role = _CALLERS[caller]
    return mint_token(
        key,
        sub=sub,
        tenant_role=role.value,
        tenant_id=str(_TENANT),
        principal_kind=kind,
        extra_claims={"azp": f"{sub}-client"} if kind else None,
    )


def _post(client: TestClient, path: str, caller: str) -> Any:
    key = make_rsa_keypair("kid-run-stop")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.post(path, headers={"Authorization": f"Bearer {_token(key, caller)}"})


async def _seed_agent_run(
    owner: str, trigger: AgentRunTrigger = AgentRunTrigger.DIRECT
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        if await session.get(Tenant, _TENANT) is None:
            session.add(Tenant(id=_TENANT, slug="tenant-a", name="Tenant A"))
        run = await create_run(
            session,
            tenant_id=_TENANT,
            identity_sub=owner,
            trigger=trigger,
            model_tier="standard",
        )
        run.status = AgentRunStatus.RUNNING.value
        await session.commit()
        return run.id


async def _agent_run_status(run_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        return (await session.get_one(AgentRun, run_id)).status


async def _seed_operation_run(owner: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = OperationRun(
            id=uuid.uuid4(),
            tenant_id=_TENANT,
            identity_sub=owner,
            origin=OperationRunOrigin.DIRECT.value,
            connector_id="vmware-rest-9.0",
            op_id="vm.list",
            status=OperationRunStatus.RUNNING.value,
            result=_RESULT,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _operation_run(run_id: uuid.UUID) -> OperationRun:
    async with get_sessionmaker()() as session:
        return await session.get_one(OperationRun, run_id)


_ALLOWED = ["owner person", "other person, tenant admin"]
_REFUSED = ["other person, operator", "agent login", "service login", "service login, tenant admin"]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", _ALLOWED)
async def test_owner_or_tenant_admin_stops_a_persons_agent_run(
    client: TestClient, caller: str
) -> None:
    run_id = await _seed_agent_run(_PERSON)

    response = _post(client, f"/api/v1/agents/runs/{run_id}/cancel", caller)

    assert response.status_code == 200, response.text
    assert await _agent_run_status(run_id) == AgentRunStatus.CANCELLED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", _REFUSED)
async def test_others_cannot_stop_a_persons_agent_run(client: TestClient, caller: str) -> None:
    run_id = await _seed_agent_run(_PERSON)

    response = _post(client, f"/api/v1/agents/runs/{run_id}/cancel", caller)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == f"agent_run_cancel_forbidden: {RUN_STOP_RULE}"
    assert await _agent_run_status(run_id) == AgentRunStatus.RUNNING.value


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [AgentRunTrigger.DIRECT, AgentRunTrigger.SCHEDULED])
async def test_an_agent_login_stops_its_own_agent_run(
    client: TestClient, trigger: AgentRunTrigger
) -> None:
    """Also a run its own schedule started: that run carries the login's sub."""
    run_id = await _seed_agent_run("agent-login", trigger)

    response = _post(client, f"/api/v1/agents/runs/{run_id}/cancel", "agent login")

    assert response.status_code == 200, response.text
    assert await _agent_run_status(run_id) == AgentRunStatus.CANCELLED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", _ALLOWED)
async def test_owner_or_tenant_admin_stops_a_persons_operation_run(
    client: TestClient, caller: str
) -> None:
    run_id = await _seed_operation_run(_PERSON)

    response = _post(client, f"/api/v1/operations/runs/{run_id}/cancel", caller)

    assert response.status_code == 200, response.text
    assert (await _operation_run(run_id)).status == OperationRunStatus.CANCELLED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", _REFUSED)
async def test_others_cannot_stop_a_persons_operation_run(client: TestClient, caller: str) -> None:
    run_id = await _seed_operation_run(_PERSON)

    response = _post(client, f"/api/v1/operations/runs/{run_id}/cancel", caller)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == f"operation_run_cancel_forbidden: {RUN_STOP_RULE}"
    row = await _operation_run(run_id)
    assert row.status == OperationRunStatus.RUNNING.value
    assert row.result == _RESULT


@pytest.mark.asyncio
async def test_a_service_login_stops_its_own_operation_run(client: TestClient) -> None:
    run_id = await _seed_operation_run("service-login")

    response = _post(client, f"/api/v1/operations/runs/{run_id}/cancel", "service login")

    assert response.status_code == 200, response.text
    assert (await _operation_run(run_id)).status == OperationRunStatus.CANCELLED.value


def test_a_platform_admin_in_the_tenant_may_stop_any_run() -> None:
    """A platform admin acts in a tenant with the ``tenant_admin`` role."""
    platform_admin = Operator(
        sub="platform-admin",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,
        role_from_platform_admin=True,
    )

    assert may_stop_run(platform_admin, _PERSON)
