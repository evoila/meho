# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak write ops put realm and role names into the path as one segment.

``keycloak.realm.update`` encodes ``realm`` and the new realm name, and
``keycloak.role_mapping.assign`` encodes each role name before the role
lookup. A ``/``, a dot or a ``%`` in a name stays part of that name. The
result of ``keycloak.role_mapping.assign`` names the roles Keycloak
returned. Keycloak is mocked with respx; every name and id is made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import respx

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import (
    KeycloakRoleNotFoundError,
    keycloak_realm_update,
    keycloak_role_mapping_assign,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)

_KC_BASE_URL = "https://keycloak-names.test.invalid"
_REALM = "demo"
_USER_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ROLE_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"

_OPERATOR = Operator(
    sub="keycloak-names-test",
    name="Keycloak Names Test Operator",
    email=None,
    raw_jwt="<keycloak-names-test-raw-jwt>",
    tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000000c1"),
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def _stub_loader(
    _target: KeycloakTargetLike, _operator: Operator
) -> KeycloakAdminCredentials:
    return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")


@pytest_asyncio.fixture
async def connector() -> AsyncIterator[KeycloakConnector]:
    instance = KeycloakConnector(credentials_loader=_stub_loader)
    yield instance
    await instance.aclose()


def _target() -> MagicMock:
    target = MagicMock(spec=KeycloakTargetLike)
    target.name = "keycloak-names-test"
    target.host = "keycloak-names.test.invalid"
    target.port = 443
    target.secret_ref = "stub/secret"
    target.auth_model = "shared_service_account"
    target.extras = {"managed_realm": _REALM}
    target.id = uuid.UUID(int=0xC1)
    target.tenant_id = uuid.UUID(int=0)
    return target


def _raw_paths(mock: respx.MockRouter) -> list[str]:
    return [call.request.url.raw_path.decode() for call in mock.calls]


@pytest.mark.asyncio
async def test_realm_update_encodes_a_realm_name_with_slashes(
    connector: KeycloakConnector,
) -> None:
    realm = f"{_REALM}/users/{_USER_UUID}"
    encoded = f"/admin/realms/{_REALM}%2Fusers%2F{_USER_UUID}"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-token", "expires_in": 300}
        )
        mock.route(method="GET").respond(200, json={"realm": realm, "enabled": True})
        mock.route(method="PUT").respond(204)
        await keycloak_realm_update(
            connector, _OPERATOR, _target(), {"realm": realm, "representation": {"enabled": True}}
        )
        paths = [p for p in _raw_paths(mock) if p.startswith("/admin/")]

    assert paths == [encoded, encoded, encoded]
    assert not any(p.startswith(f"/admin/realms/{_REALM}/users") for p in paths)


@pytest.mark.asyncio
async def test_realm_update_encodes_the_new_realm_name_for_the_read_back(
    connector: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-token", "expires_in": 300}
        )
        mock.route(method="GET").respond(200, json={"realm": _REALM})
        mock.route(method="PUT").respond(204)
        await keycloak_realm_update(
            connector,
            _OPERATOR,
            _target(),
            {"realm": _REALM, "representation": {"realm": "../master"}},
        )
        paths = [p for p in _raw_paths(mock) if p.startswith("/admin/")]

    assert paths == [f"/admin/realms/{_REALM}"] * 2 + ["/admin/realms/%2E%2E%2Fmaster"]


@pytest.mark.asyncio
async def test_role_mapping_assign_does_not_decode_a_percent_encoded_role_name(
    connector: KeycloakConnector,
) -> None:
    role_name = "%61dmin%2F..%2Fx"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-token", "expires_in": 300}
        )
        lookup = mock.get(path__startswith=f"/admin/realms/{_REALM}/roles/").respond(
            200, json={"id": _ROLE_UUID, "name": role_name}
        )
        grant = mock.post(f"/admin/realms/{_REALM}/users/{_USER_UUID}/role-mappings/realm").respond(
            204
        )
        result = await keycloak_role_mapping_assign(
            connector, _OPERATOR, _target(), {"id": _USER_UUID, "roles": [role_name]}
        )

    assert lookup.calls.last.request.url.raw_path.decode() == (
        f"/admin/realms/{_REALM}/roles/%2561dmin%252F%2E%2E%252Fx"
    )
    assert json.loads(grant.calls.last.request.content) == [{"id": _ROLE_UUID, "name": role_name}]
    assert result["assigned_roles"] == [role_name]


@pytest.mark.asyncio
async def test_role_mapping_assign_refuses_a_role_of_another_name(
    connector: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-token", "expires_in": 300}
        )
        mock.get(path__startswith=f"/admin/realms/{_REALM}/roles/").respond(
            200, json={"id": _ROLE_UUID, "name": "realm-admin"}
        )
        grant = mock.post(path__regex=r"/role-mappings/realm$").respond(204)
        with pytest.raises(KeycloakRoleNotFoundError):
            await keycloak_role_mapping_assign(
                connector, _OPERATOR, _target(), {"id": _USER_UUID, "roles": ["viewer"]}
            )

    assert not grant.called


@pytest.mark.asyncio
async def test_role_mapping_assign_returns_the_names_keycloak_returned(
    connector: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post("/realms/master/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{_REALM}/roles/viewer").respond(
            200, json={"id": _ROLE_UUID, "name": "viewer"}
        )
        mock.post(f"/admin/realms/{_REALM}/users/{_USER_UUID}/role-mappings/realm").respond(204)
        result = await keycloak_role_mapping_assign(
            connector, _OPERATOR, _target(), {"id": _USER_UUID, "roles": [" viewer ", ""]}
        )

    assert result == {"id": _USER_UUID, "username": None, "assigned_roles": ["viewer"]}
