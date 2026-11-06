# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console grant writes and agent-principal register are for people.

A console session whose access token is a service login with the
``tenant_admin`` role gets 403 ``human_principal_required`` on four
routes, and nothing is written:

* ``POST /ui/agents/grants/create``
* ``POST /ui/agents/grants/elevate``
* ``POST /ui/grants/{kind}/{grant_id}/revoke``
* ``POST /ui/agents/principals/register``

An agent login is refused too (MEHO caps its role at ``operator``). A
person with ``tenant_admin`` still uses all four routes; that is covered by
``test_create_success_redirects``, ``test_elevate_success_redirects``
(``test_ui_agent_grants.py``), ``test_admin_revoke_roundtrip_changes_both_grant_kinds``
(``test_ui_unified_grants.py``) and ``test_register_persists_and_redirects``
(``test_ui_agent_principals.py``).

Standing grants for an op with a role floor (``targets.register``, the
``topology.*`` writes) in the console: a person with the ``operator``
role gets 403 on create and on renew; a tenant admin makes the grant.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import TenantRole

# Importing the ops module registers the role floor of targets.register.
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, AgentPrincipal, ServicePrincipalGrant
from tests._oidc_jwt_helpers import mint_token
from tests.test_ui_agent_grants import (
    _OP_A,
    _TENANT_A,
    _admin_token,
    _authenticated_client,
    _bff_env,  # noqa: F401  (autouse fixture)
    _csrf_headers,
    _future_iso,
    _make_keypair_and_jwks,
    _operator_token,
    _register_principal,
    _seed_grant,
    _seed_session_sync,
    _seed_tenant,
)
from tests.test_ui_unified_grants import _seed_service


def _machine_client(kind: str) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    sub = f"{kind}:deploy-bot"
    token = mint_token(
        keypair,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        principal_kind=kind,
    )
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=sub)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)


async def _counts() -> tuple[int, int, int, int]:
    async with get_sessionmaker()() as session:

        async def count(model: Any, *where: Any) -> int:
            query = select(func.count()).select_from(model).where(*where)
            return int((await session.execute(query)).scalar_one())

        return (
            await count(AgentPermission),
            await count(AgentPrincipal),
            await count(ServicePrincipalGrant),
            await count(ServicePrincipalGrant, ServicePrincipalGrant.revoked_at.is_not(None)),
        )


def _grant_form(**extra: str) -> dict[str, str]:
    return {
        "principal_sub": "agent-new",
        "op_pattern": "vault.kv.*",
        "target_scope": "",
        "verdict": "auto-execute",
        **extra,
    }


def _post(route: str, client: Any, csrf: str, ids: dict[str, str]) -> Any:
    headers = _csrf_headers(csrf)
    if route == "create":
        return client.post("/ui/agents/grants/create", data=_grant_form(), headers=headers)
    if route == "elevate":
        form = _grant_form(expires_at=_future_iso())
        return client.post("/ui/agents/grants/elevate", data=form, headers=headers)
    if route == "revoke-agent":
        return client.post(f"/ui/grants/agent/{ids['agent']}/revoke", headers=headers)
    if route == "revoke-service":
        return client.post(f"/ui/grants/service/{ids['service']}/revoke", headers=headers)
    return client.post(
        "/ui/agents/principals/register", data={"name": "new-agent"}, headers=headers
    )


_ROUTES = ("create", "elevate", "revoke-agent", "revoke-service", "register")


@pytest.mark.parametrize("route", _ROUTES)
@pytest.mark.parametrize("kind", ["service", "agent"])
def test_a_machine_console_session_is_refused(kind: str, route: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_principal(_TENANT_A, "agent-new")
    ids = {
        "agent": str(_seed_grant(tenant_id=_TENANT_A, principal_sub="agent-new")),
        "service": str(_seed_service(_TENANT_A)),
    }
    before = asyncio.run(_counts())
    client, mock, csrf = _machine_client(kind)
    try:
        response = _post(route, client, csrf, ids)
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    if kind == "service":
        assert response.json() == {"detail": "human_principal_required"}
    assert asyncio.run(_counts()) == before


def _person_client(role: TenantRole) -> Any:
    keypair, jwks = _make_keypair_and_jwks()
    token = _admin_token(keypair) if role is TenantRole.TENANT_ADMIN else _operator_token(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    return _authenticated_client(session_id=session_id, jwks=jwks, with_csrf=True)


_FLOORED_FORM = {
    "kind": "service",
    "principal_sub": "svc:deploy-bot",
    "op": TARGETS_REGISTER_OP_ID,
    "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
    "reason": "unattended build",
}


async def _service_grant_ops() -> list[str]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ServicePrincipalGrant.op_id))).scalars())


@pytest.mark.parametrize("route", ["create", "renew"])
def test_an_operator_cannot_make_a_floored_grant_in_the_console(route: str) -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _person_client(TenantRole.OPERATOR)
    try:
        if route == "create":
            url, form = "/ui/grants/create", _FLOORED_FORM
        else:
            url = "/ui/grants/service/renew-delete-grants"
            form = {"principal_sub": "svc:deploy-bot", "expires_at": _future_iso()}
        response = client.post(url, data=form, headers=_csrf_headers(csrf))
    finally:
        mock.stop()

    assert response.status_code == 403, response.text
    assert asyncio.run(_service_grant_ops()) == []


def test_a_tenant_admin_makes_a_floored_grant_in_the_console() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    client, mock, csrf = _person_client(TenantRole.TENANT_ADMIN)
    try:
        response = client.post("/ui/grants/create", data=_FLOORED_FORM, headers=_csrf_headers(csrf))
    finally:
        mock.stop()

    assert response.status_code == 204, response.text
    assert asyncio.run(_service_grant_ops()) == [TARGETS_REGISTER_OP_ID]
