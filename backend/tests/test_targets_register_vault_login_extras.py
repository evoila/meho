# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``targets.register`` applies the platform-admin rule for the Vault login role.

The operation calls the same create handler as ``POST /api/v1/targets``,
so a caller without ``platform_admin`` cannot set ``vault_role`` or
``vault_mount`` through it either. See
:mod:`meho_backplane.targets.vault_login_extras`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.db.models import Tenant
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.targets.vault_login_extras import VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# MCP calls come with a token; these tests check executed writes (#3980).
pytestmark = pytest.mark.usefixtures("person_token_writes_run_at_once")

_ROLE_EXTRAS = {"vault_role": "narrow-role", "vault_mount": "jwt-other"}


@pytest_asyncio.fixture
async def _seeded_tenant() -> AsyncIterator[None]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="op-tenant", name="Op Tenant"))
    yield


def _register_call(client: TestClient, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_targets_register", "arguments": arguments},
        },
    )


async def _stored_extras(name: str) -> list[dict[str, Any]]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(TargetORM.extras).where(
                TargetORM.tenant_id == OPERATOR_TENANT_ID, TargetORM.name == name
            )
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
@pytest.mark.parametrize(
    "extras",
    [{"vault_role": "narrow-role"}, {"vault_mount": "jwt-other"}, _ROLE_EXTRAS],
    ids=["role", "mount", "both"],
)
async def test_register_with_vault_login_keys_needs_platform_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
    extras: dict[str, Any],
) -> None:
    client, _op = client_with_operator
    body = _register_call(
        client,
        {"name": "vault-a", "product": "vault", "host": "vault.example", "extras": extras},
    ).json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert VAULT_LOGIN_ROLE_REQUIRES_PLATFORM_ADMIN in body["error"]["message"]
    assert await _stored_extras("vault-a") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("client_with_operator", [TenantRole.TENANT_ADMIN], indirect=True)
async def test_register_with_other_extras_stays_open_to_tenant_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    client, _op = client_with_operator
    created = _register_call(
        client,
        {
            "name": "vault-b",
            "product": "vault",
            "host": "vault.example",
            "extras": {"sso_realm": "lab"},
        },
    ).json()["result"]
    assert created["isError"] is False
    assert await _stored_extras("vault-b") == [{"sso_realm": "lab"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("client_with_operator", [(TenantRole.TENANT_ADMIN, True)], indirect=True)
async def test_register_with_vault_login_keys_as_platform_admin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    _seeded_tenant: None,
) -> None:
    client, _op = client_with_operator
    created = _register_call(
        client,
        {"name": "vault-c", "product": "vault", "host": "vault.example", "extras": _ROLE_EXTRAS},
    ).json()["result"]
    assert created["isError"] is False
    assert await _stored_extras("vault-c") == [_ROLE_EXTRAS]
