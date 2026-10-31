# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Agents act with operator rights. Tenant-admin work is for people.

What these tests check:

* MEHO registers a new agent in Keycloak with ``tenant_role=operator``.
* An agent token that still says ``tenant_admin`` (an agent registered
  earlier) is read as ``operator``. People and service logins keep the
  role their token carries.
* So an agent token gets 403 ``insufficient_role`` on the tenant-admin
  write routes, one case per route family. A person with
  ``tenant_admin`` passes the same role check.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.auth.agent_principals import AgentPrincipalCreate, AgentPrincipalService
from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant, TenantConvention
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000a6e7")

_CONVENTION: dict[str, Any] = {
    "slug": "change-window",
    "title": "Change window",
    "body": "Changes run only in the agreed change window.",
    "kind": "operational",
    "priority": 32767,
}

#: One or more tenant-admin write routes per route family:
#: (case id, method, path, JSON body or None).
_ADMIN_WRITES: list[tuple[str, str, str, dict[str, Any] | None]] = [
    (
        "operation-edit",
        "PATCH",
        "/api/v1/connectors/demo/operations/demo.item.update",
        {"safety_level": "safe", "requires_approval": False},
    ),
    ("connector-ingest", "POST", "/api/v1/connectors/ingest", {}),
    ("connector-enable", "POST", "/api/v1/connectors/demo/enable", None),
    ("connector-disable", "POST", "/api/v1/connectors/demo/disable", None),
    ("connector-delete", "DELETE", "/api/v1/connectors/demo", None),
    ("target-create", "POST", "/api/v1/targets", {}),
    ("target-edit", "PATCH", "/api/v1/targets/demo", {}),
    ("target-delete", "DELETE", "/api/v1/targets/demo", None),
    ("event-source-create", "POST", "/api/v1/event-sources", {}),
    ("doc-collection-create", "POST", "/api/v1/doc_collections", {}),
    ("runner-write-allowlist", "POST", "/api/v1/runner-principals/demo/write-allowlist", {}),
    ("scheduler-trigger-create", "POST", "/api/v1/scheduler/triggers", {}),
    (
        "scheduler-trigger-cancel",
        "DELETE",
        f"/api/v1/scheduler/triggers/{uuid.uuid4()}",
        None,
    ),
    ("agent-definition-create", "POST", "/api/v1/agents", {}),
    (
        "agent-definition-edit",
        "PATCH",
        "/api/v1/agents/demo",
        {"identity_ref": "agent:other-bot"},
    ),
    ("agent-definition-delete", "DELETE", "/api/v1/agents/demo", None),
    ("flight-recorder-policy", "PATCH", "/api/v1/tenants/flight-recorder-policy", {}),
    ("agent-grant-revoke", "DELETE", f"/api/v1/agents/grants/{uuid.uuid4()}", None),
    ("agent-principal-revoke", "DELETE", "/api/v1/agent-principals/demo/revoke", None),
    ("convention-create", "POST", "/api/v1/conventions", _CONVENTION),
    ("convention-edit", "PATCH", "/api/v1/conventions/demo", {"priority": 32767}),
    ("convention-delete", "DELETE", "/api/v1/conventions/demo", None),
]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-agent-role")
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _seed_tenant() -> None:
    async with get_sessionmaker()() as session:
        session.add(Tenant(id=_TENANT, slug="tenant-agent-role", name="Tenant agent role"))
        await session.commit()


def _admin_token(key: Any, *, principal_kind: str | None) -> str:
    """A token that says ``tenant_admin``; *principal_kind* None is a person."""
    return mint_token(
        key,
        sub=f"{principal_kind or 'person'}-sub",
        tenant_id=str(_TENANT),
        tenant_role=TenantRole.TENANT_ADMIN.value,
        principal_kind=principal_kind,
    )


async def _send(
    ac: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None, token: str
) -> httpx.Response:
    return await ac.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal_kind", "claimed", "expected"),
    [
        ("agent", TenantRole.TENANT_ADMIN, TenantRole.OPERATOR),
        ("agent", TenantRole.OPERATOR, TenantRole.OPERATOR),
        ("agent", TenantRole.READ_ONLY, TenantRole.READ_ONLY),
        (None, TenantRole.TENANT_ADMIN, TenantRole.TENANT_ADMIN),
        ("service", TenantRole.TENANT_ADMIN, TenantRole.TENANT_ADMIN),
    ],
    ids=["agent-admin", "agent-operator", "agent-read-only", "person", "service"],
)
async def test_agent_token_acts_with_at_most_operator_rights(
    principal_kind: str | None, claimed: TenantRole, expected: TenantRole
) -> None:
    key = make_rsa_keypair("kid-role-cap")
    token = mint_token(key, tenant_role=claimed.value, principal_kind=principal_kind)
    with respx.mock as router:
        mock_discovery_and_jwks(router, public_jwks(key))
        operator = await verify_jwt_for_audience(f"Bearer {token}", expected_audience=AUDIENCE)
    assert operator.tenant_role is expected


@pytest.mark.asyncio
async def test_new_agent_is_registered_with_the_operator_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_tenant()
    keycloak = AsyncMock()
    keycloak.create_client = AsyncMock(return_value="kc-internal-id")
    keycloak.get_client_secret = AsyncMock(return_value="generated-secret")
    keycloak.__aenter__ = AsyncMock(return_value=keycloak)
    keycloak.__aexit__ = AsyncMock(return_value=None)

    async def _no_vault_write(
        identity_ref: str, client_secret: str, *, tenant_id: uuid.UUID
    ) -> str:
        return f"secret/data/agents/{identity_ref}/credentials"

    monkeypatch.setattr("meho_backplane.auth.agent_principals.write_agent_secret", _no_vault_write)
    with patch(
        "meho_backplane.auth.agent_principals.KeycloakAdminClient.from_settings",
        MagicMock(return_value=keycloak),
    ):
        await AgentPrincipalService().register(
            _TENANT, "person-sub", AgentPrincipalCreate(name="deploy-bot")
        )

    assert keycloak.create_client.await_args is not None
    assert keycloak.create_client.await_args.kwargs["tenant_role"] == TenantRole.OPERATOR.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [case[1:] for case in _ADMIN_WRITES],
    ids=[case[0] for case in _ADMIN_WRITES],
)
async def test_agent_token_is_refused_on_tenant_admin_writes(
    _client: tuple[httpx.AsyncClient, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    ac, key = _client
    await _seed_tenant()

    response = await _send(ac, method, path, body, _admin_token(key, principal_kind="agent"))

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "insufficient_role"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [case[1:] for case in _ADMIN_WRITES],
    ids=[case[0] for case in _ADMIN_WRITES],
)
async def test_person_with_tenant_admin_passes_the_same_role_check(
    _client: tuple[httpx.AsyncClient, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """The route runs for a person (any answer but 401 / 403)."""
    ac, key = _client
    await _seed_tenant()

    response = await _send(ac, method, path, body, _admin_token(key, principal_kind=None))

    assert response.status_code not in (401, 403), response.text


@pytest.mark.asyncio
async def test_agent_cannot_change_conventions_but_a_person_can(
    _client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = _client
    await _seed_tenant()
    agent = _admin_token(key, principal_kind="agent")
    person = _admin_token(key, principal_kind=None)
    item = f"/api/v1/conventions/{_CONVENTION['slug']}"

    assert (await _send(ac, "POST", "/api/v1/conventions", _CONVENTION, agent)).status_code == 403
    created = await _send(ac, "POST", "/api/v1/conventions", _CONVENTION, person)
    assert created.status_code == 201, created.text

    assert (await _send(ac, "PATCH", item, {"title": "Changed"}, agent)).status_code == 403
    assert (await _send(ac, "DELETE", item, None, agent)).status_code == 403
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(TenantConvention).where(TenantConvention.tenant_id == _TENANT)
            )
        ).scalar_one()
    assert row.title == _CONVENTION["title"]

    assert (await _send(ac, "PATCH", item, {"title": "Changed"}, person)).status_code == 200
    assert (await _send(ac, "DELETE", item, None, person)).status_code == 204
