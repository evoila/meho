# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_agents_grant_create`` and ``meho_agent_principals_register`` are for people.

* A service or runner login with ``tenant_admin`` and ``mcp:admin`` gets
  ``-32602`` with ``human_principal_required`` on both tools, and nothing
  is written.
* The same holds when MEHO infers the kind from the token: a
  client-credentials token without a ``principal_kind`` claim.
* A person with ``tenant_admin`` still creates a grant.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, AgentPrincipal
from meho_backplane.main import app
from meho_backplane.mcp.auth import mcp_resource_uri, verify_mcp_jwt, verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_AGENT_CLIENT_ID = "agent-inventory"

_CALLS: dict[str, dict[str, Any]] = {
    "meho_agents_grant_create": {
        "principal_sub": _AGENT_CLIENT_ID,
        "op_pattern": "vault.kv.*",
        "verdict": "auto-execute",
        "expires_at": "2099-01-01T00:00:00Z",
    },
    "meho_agent_principals_register": {"name": "new-agent"},
}


def _operator(kind: PrincipalKind) -> Operator:
    return Operator(
        sub=f"{kind.value}:deploy-bot" if kind is not PrincipalKind.USER else "person-1",
        name="Test",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=kind,
        runner_id=uuid.uuid4() if kind is PrincipalKind.RUNNER else None,
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


def _client_for(operator: Operator) -> Iterator[TestClient]:
    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _call(client: TestClient, name: str) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": _CALLS[name]},
    }
    response = post_mcp(client, body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def _seed_agent_principal() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPrincipal(
                tenant_id=OPERATOR_TENANT_ID,
                name="inventory",
                keycloak_client_id=_AGENT_CLIENT_ID,
                keycloak_internal_id=str(uuid.uuid4()),
                owner_sub="person-1",
                created_by_sub="person-1",
            )
        )


async def _count(model: type[AgentPermission] | type[AgentPrincipal]) -> int:
    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def _assert_refused(body: dict[str, Any], tool: str) -> None:
    assert "error" in body, body
    assert body["error"]["code"] == INVALID_PARAMS
    message = body["error"]["message"]
    assert message.startswith(f"human_principal_required: {tool} is for people only"), message


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PrincipalKind.SERVICE, PrincipalKind.RUNNER])
@pytest.mark.parametrize("tool", sorted(_CALLS))
async def test_a_machine_login_with_tenant_admin_is_refused(kind: PrincipalKind, tool: str) -> None:
    await _seed_agent_principal()
    for client in _client_for(_operator(kind)):
        body = _call(client, tool)

    _assert_refused(body, tool)
    assert await _count(AgentPermission) == 0
    assert await _count(AgentPrincipal) == 1


@pytest.fixture
def inferred_service_operator() -> Iterator[Operator]:
    """A client-credentials token with no ``principal_kind`` claim, read by MEHO."""
    key = make_rsa_keypair("kid-mcp-inferred")
    token = mint_token(
        key,
        sub=str(uuid.uuid4()),
        tenant_id=str(OPERATOR_TENANT_ID),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        audience=mcp_resource_uri(),
        scopes=[MCP_ADMIN_SCOPE],
        extra_claims={"azp": "deploy-bot"},
    )
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        yield asyncio.run(verify_mcp_jwt(f"Bearer {token}", None))


@pytest.mark.parametrize("tool", sorted(_CALLS))
def test_an_inferred_service_login_is_refused(
    inferred_service_operator: Operator, tool: str
) -> None:
    assert inferred_service_operator.principal_kind is PrincipalKind.SERVICE
    assert inferred_service_operator.tenant_role is TenantRole.TENANT_ADMIN
    for client in _client_for(inferred_service_operator):
        body = _call(client, tool)

    _assert_refused(body, tool)


@pytest.mark.asyncio
async def test_a_person_with_tenant_admin_still_creates_a_grant() -> None:
    await _seed_agent_principal()
    for client in _client_for(_operator(PrincipalKind.USER)):
        body = _call(client, "meho_agents_grant_create")

    assert "result" in body, body
    assert body["result"]["isError"] is False
    assert await _count(AgentPermission) == 1
