# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Keycloak Admin REST client HTTP layer (G11.2-T1 #815).

Exercises create / disable / delete and the status-code mapping against a
mocked Keycloak Admin API (respx) — no running Keycloak required. Covers the
Location-header parse on ``create_client`` and the orphan-rollback
``delete_client`` added for the unreachable-kill-switch fix.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakClientConflictError,
    KeycloakClientNotFoundError,
)

_ADMIN_URL = "https://kc.test/admin/realms/meho"
_TOKEN_URL = "https://kc.test/realms/meho/protocol/openid-connect/token"
_INTERNAL_ID = "11111111-1111-1111-1111-111111111111"


def _client() -> KeycloakAdminClient:
    return KeycloakAdminClient(
        admin_url=_ADMIN_URL,
        token_url=_TOKEN_URL,
        client_id="meho-admin",
        client_secret="s3cr3t",
    )


def _mock_token(r: respx.MockRouter) -> None:
    r.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "admin-tok"}))


@pytest.mark.asyncio
async def test_create_client_parses_internal_id_from_location() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.post(f"{_ADMIN_URL}/clients").mock(
            return_value=httpx.Response(
                201, headers={"Location": f"{_ADMIN_URL}/clients/{_INTERNAL_ID}"}
            )
        )
        async with _client() as kc:
            internal_id = await kc.create_client(
                client_id="agent:bot", name="bot", tenant_id="t", owner_sub="o"
            )
    assert internal_id == _INTERNAL_ID


@pytest.mark.asyncio
async def test_create_client_conflict_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.post(f"{_ADMIN_URL}/clients").mock(return_value=httpx.Response(409))
        async with _client() as kc:
            with pytest.raises(KeycloakClientConflictError):
                await kc.create_client(
                    client_id="agent:bot", name="bot", tenant_id="t", owner_sub="o"
                )


@pytest.mark.asyncio
async def test_create_client_missing_location_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.post(f"{_ADMIN_URL}/clients").mock(return_value=httpx.Response(201))
        async with _client() as kc:
            with pytest.raises(KeycloakAdminError):
                await kc.create_client(
                    client_id="agent:bot", name="bot", tenant_id="t", owner_sub="o"
                )


@pytest.mark.asyncio
async def test_disable_client_ok() -> None:
    with respx.mock as r:
        _mock_token(r)
        route = r.put(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(return_value=httpx.Response(204))
        async with _client() as kc:
            await kc.disable_client(_INTERNAL_ID)
    assert route.called


@pytest.mark.asyncio
async def test_disable_client_not_found_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.put(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(return_value=httpx.Response(404))
        async with _client() as kc:
            with pytest.raises(KeycloakClientNotFoundError):
                await kc.disable_client(_INTERNAL_ID)


@pytest.mark.asyncio
async def test_delete_client_ok() -> None:
    with respx.mock as r:
        _mock_token(r)
        route = r.delete(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(
            return_value=httpx.Response(204)
        )
        async with _client() as kc:
            await kc.delete_client(_INTERNAL_ID)
    assert route.called


@pytest.mark.asyncio
async def test_delete_client_not_found_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.delete(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(return_value=httpx.Response(404))
        async with _client() as kc:
            with pytest.raises(KeycloakClientNotFoundError):
                await kc.delete_client(_INTERNAL_ID)


@pytest.mark.asyncio
async def test_delete_client_unexpected_status_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.delete(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(return_value=httpx.Response(500))
        async with _client() as kc:
            with pytest.raises(KeycloakAdminError):
                await kc.delete_client(_INTERNAL_ID)
