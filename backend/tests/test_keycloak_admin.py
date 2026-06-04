# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the Keycloak Admin REST client HTTP layer (G11.2-T1 #815).

Exercises create / disable / delete and the status-code mapping against a
mocked Keycloak Admin API (respx) — no running Keycloak required. Covers the
Location-header parse on ``create_client`` and the orphan-rollback
``delete_client`` added for the unreachable-kill-switch fix.
"""

from __future__ import annotations

import json

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
async def test_aenter_auth_failure_closes_client() -> None:
    """A failed auth in ``__aenter__`` must not leak the open AsyncClient."""
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(401))
        kc = _client()
        with pytest.raises(KeycloakAdminError):
            await kc.__aenter__()
        assert kc._http is None
        assert kc._token is None


async def _create_bot(kc: KeycloakAdminClient) -> str:
    """Invoke ``create_client`` with the agent-principal argument shape."""
    return await kc.create_client(
        client_id="agent:bot",
        name="bot",
        tenant_id="11111111-1111-1111-1111-111111111111",
        owner_sub="o",
        audience="meho-backplane",
        tenant_role="tenant_admin",
    )


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
            internal_id = await _create_bot(kc)
    assert internal_id == _INTERNAL_ID


@pytest.mark.asyncio
async def test_create_client_provisions_audience_scopes_and_tenant_claims() -> None:
    """The created client carries the mapper + scope set #1487 requires.

    Without these, the agent's ``client_credentials`` token lacks ``aud``,
    ``sub``, ``tenant_id`` and ``tenant_role`` and is rejected fail-closed
    at ``verify_jwt_for_audience`` before any operation dispatches.
    """
    with respx.mock as r:
        _mock_token(r)
        route = r.post(f"{_ADMIN_URL}/clients").mock(
            return_value=httpx.Response(
                201, headers={"Location": f"{_ADMIN_URL}/clients/{_INTERNAL_ID}"}
            )
        )
        async with _client() as kc:
            await _create_bot(kc)

    body = json.loads(route.calls[0].request.content)

    # Default client scopes carry the basic/sub mapper Keycloak 25+ moved
    # out of the hardcoded token path; created-via-API clients do not
    # inherit them unless the POST names them explicitly.
    assert body["defaultClientScopes"] == ["basic", "roles", "web-origins", "acr"]

    mappers = {m["name"]: m for m in body["protocolMappers"]}

    audience = mappers["audience-mapper"]
    assert audience["protocolMapper"] == "oidc-audience-mapper"
    assert audience["config"]["included.custom.audience"] == "meho-backplane"
    assert audience["config"]["access.token.claim"] == "true"

    tenant_id = mappers["tenant-id-claim"]
    assert tenant_id["protocolMapper"] == "oidc-hardcoded-claim-mapper"
    assert tenant_id["config"]["claim.name"] == "tenant_id"
    assert tenant_id["config"]["claim.value"] == "11111111-1111-1111-1111-111111111111"

    tenant_role = mappers["tenant-role-claim"]
    assert tenant_role["config"]["claim.name"] == "tenant_role"
    assert tenant_role["config"]["claim.value"] == "tenant_admin"

    principal_kind = mappers["principal-kind-claim"]
    assert principal_kind["config"]["claim.name"] == "principal_kind"
    assert principal_kind["config"]["claim.value"] == "agent"
    # Agent tokens are access-token only (no ID/userinfo token on a
    # client_credentials grant); every claim mapper must target the
    # access token or the claim never lands.
    for mapper in body["protocolMappers"]:
        assert mapper["config"]["access.token.claim"] == "true"


@pytest.mark.asyncio
async def test_create_client_conflict_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.post(f"{_ADMIN_URL}/clients").mock(return_value=httpx.Response(409))
        async with _client() as kc:
            with pytest.raises(KeycloakClientConflictError):
                await _create_bot(kc)


@pytest.mark.asyncio
async def test_create_client_missing_location_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.post(f"{_ADMIN_URL}/clients").mock(return_value=httpx.Response(201))
        async with _client() as kc:
            with pytest.raises(KeycloakAdminError):
                await _create_bot(kc)


@pytest.mark.asyncio
async def test_disable_client_ok() -> None:
    """disable_client GETs the current representation before PUTting it back.

    A partial PUT (only ``{"enabled": false}``) would wipe custom attributes
    like ``kind=agent``, breaking the principal-kind discriminator.
    """
    representation = {"id": _INTERNAL_ID, "enabled": True, "attributes": {"kind": "agent"}}
    with respx.mock as r:
        _mock_token(r)
        get_route = r.get(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(
            return_value=httpx.Response(200, json=representation)
        )
        put_route = r.put(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(
            return_value=httpx.Response(204)
        )
        async with _client() as kc:
            await kc.disable_client(_INTERNAL_ID)
    assert get_route.called
    assert put_route.called
    sent_body = json.loads(put_route.calls[0].request.content)
    assert sent_body["enabled"] is False
    assert sent_body["attributes"] == {"kind": "agent"}


@pytest.mark.asyncio
async def test_disable_client_not_found_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.get(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}").mock(return_value=httpx.Response(404))
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


@pytest.mark.asyncio
async def test_get_client_secret_returns_value() -> None:
    """``get_client_secret`` extracts the ``value`` field (G0.19-T2 #1478)."""
    with respx.mock as r:
        _mock_token(r)
        r.get(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}/client-secret").mock(
            return_value=httpx.Response(200, json={"type": "secret", "value": "gen-secret"})
        )
        async with _client() as kc:
            secret = await kc.get_client_secret(_INTERNAL_ID)
    assert secret == "gen-secret"


@pytest.mark.asyncio
async def test_get_client_secret_not_found_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.get(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}/client-secret").mock(
            return_value=httpx.Response(404)
        )
        async with _client() as kc:
            with pytest.raises(KeycloakClientNotFoundError):
                await kc.get_client_secret(_INTERNAL_ID)


@pytest.mark.asyncio
async def test_get_client_secret_empty_value_raises() -> None:
    """An empty ``value`` (public client / misconfig) is an admin error."""
    with respx.mock as r:
        _mock_token(r)
        r.get(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}/client-secret").mock(
            return_value=httpx.Response(200, json={"type": "secret", "value": ""})
        )
        async with _client() as kc:
            with pytest.raises(KeycloakAdminError):
                await kc.get_client_secret(_INTERNAL_ID)


@pytest.mark.asyncio
async def test_get_client_secret_unexpected_status_raises() -> None:
    with respx.mock as r:
        _mock_token(r)
        r.get(f"{_ADMIN_URL}/clients/{_INTERNAL_ID}/client-secret").mock(
            return_value=httpx.Response(500)
        )
        async with _client() as kc:
            with pytest.raises(KeycloakAdminError):
                await kc.get_client_secret(_INTERNAL_ID)
