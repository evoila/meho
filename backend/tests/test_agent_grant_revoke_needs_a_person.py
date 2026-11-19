# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Only a person removes an agent grant whose verdict is deny or needs-approval.

A ``deny`` or ``needs-approval`` grant limits an agent. On REST
(``DELETE /api/v1/agents/grants/{id}``) and in the console
(``POST /ui/agents/grants/{id}/revoke``):

* a service login with ``tenant_admin`` gets 403 ``human_principal_required``
  for such a grant, and the row stays;
* the same login removes an ``auto-execute`` grant (that only narrows the
  agent);
* a person with ``tenant_admin`` removes a ``deny`` grant.

The MCP tool ``meho_agents_grant_revoke`` is covered in
``test_mcp_grant_writes_need_a_person.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.agents.grants import AgentGrantService, GrantRevokeNeedsPersonError
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission
from meho_backplane.main import app
from tests._oidc_jwt_helpers import mint_token, mock_discovery_and_jwks
from tests.test_ui_agent_grants import (
    _OP_A,
    _TENANT_A,
    _TENANT_B,
    _admin_token,
    _authenticated_client,
    _bff_env,  # noqa: F401  (autouse fixture)
    _csrf_headers,
    _make_keypair_and_jwks,
    _seed_grant,
    _seed_session_sync,
    _seed_tenant,
)

_LIMITING = ("deny", "needs-approval")
_SERVICE_SUB = "service:deploy-bot"


def _service_token(keypair: Any) -> str:
    return mint_token(
        keypair,
        sub=_SERVICE_SUB,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        principal_kind="service",
    )


def _exists(grant_id: uuid.UUID) -> bool:
    async def _do() -> bool:
        async with get_sessionmaker()() as session:
            found = await session.scalar(
                select(AgentPermission.id).where(AgentPermission.id == grant_id)
            )
            return found is not None

    return asyncio.run(_do())


def _rest_delete(token_for: str, grant_id: uuid.UUID) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    token = _service_token(keypair) if token_for == "service" else _admin_token(keypair)
    with respx.mock(assert_all_called=False) as router:
        mock_discovery_and_jwks(router, jwks)
        return TestClient(app).delete(
            f"/api/v1/agents/grants/{grant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )


def _console_revoke(token_for: str, grant_id: uuid.UUID) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    if token_for == "service":
        token, sub = _service_token(keypair), _SERVICE_SUB
    else:
        token, sub = _admin_token(keypair), _OP_A
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=sub)
    client, mock, csrf = _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)
    try:
        return client.post(f"/ui/agents/grants/{grant_id}/revoke", headers=_csrf_headers(csrf))
    finally:
        mock.stop()


_SURFACES = {"rest": _rest_delete, "console": _console_revoke}


@pytest.mark.parametrize("surface", sorted(_SURFACES))
@pytest.mark.parametrize("verdict", _LIMITING)
def test_a_service_login_cannot_remove_a_limiting_grant(surface: str, verdict: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, verdict=verdict)

    response = _SURFACES[surface]("service", grant_id)

    assert response.status_code == 403, response.text
    assert response.json()["detail"].startswith("human_principal_required"), response.text
    assert _exists(grant_id)


@pytest.mark.parametrize("surface", sorted(_SURFACES))
def test_a_service_login_removes_an_auto_execute_grant(surface: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, verdict="auto-execute")

    response = _SURFACES[surface]("service", grant_id)

    assert response.status_code == 204, response.text
    assert not _exists(grant_id)


@pytest.mark.parametrize("surface", sorted(_SURFACES))
@pytest.mark.parametrize("verdict", _LIMITING)
def test_a_person_removes_a_limiting_grant(surface: str, verdict: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, verdict=verdict)

    response = _SURFACES[surface]("person", grant_id)

    assert response.status_code == 204, response.text
    assert not _exists(grant_id)


def test_the_rest_refusal_names_the_person_path() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, verdict="deny")

    detail = _rest_delete("service", grant_id).json()["detail"]

    assert "`meho agent grant revoke <grant-id>`" in detail
    assert "auto-execute" in detail


@pytest.mark.parametrize("surface", sorted(_SURFACES))
def test_a_limiting_grant_of_another_tenant_is_404_for_a_service_login(surface: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _seed_tenant(_TENANT_B, "tenant-b")
    grant_id = _seed_grant(tenant_id=_TENANT_B, verdict="deny")

    response = _SURFACES[surface]("service", grant_id)

    assert response.status_code == 404, response.text
    assert _exists(grant_id)


@pytest.mark.parametrize("verdict", _LIMITING)
def test_the_service_keeps_a_limiting_row_when_limited_to_auto_execute(verdict: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    grant_id = _seed_grant(tenant_id=_TENANT_A, verdict=verdict)

    with pytest.raises(GrantRevokeNeedsPersonError) as refused:
        asyncio.run(AgentGrantService().revoke(_TENANT_A, grant_id, auto_execute_only=True))

    assert refused.value.verdict == verdict
    assert _exists(grant_id)


def test_the_service_limited_to_auto_execute_returns_false_for_an_absent_row() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")

    deleted = asyncio.run(
        AgentGrantService().revoke(_TENANT_A, uuid.uuid4(), auto_execute_only=True)
    )

    assert deleted is False
