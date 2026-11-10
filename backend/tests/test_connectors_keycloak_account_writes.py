# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak account writes: client roles, user details, one stored credential.

Drives ``keycloak.role_mapping.client.assign``, ``keycloak.role_mapping.client.remove``,
``keycloak.user.update`` and ``keycloak.user.credential.delete`` through the real
dispatch stack against a respx-mocked Keycloak Admin REST API. No running
Keycloak and no Vault: the admin-credential loader is a stub and the connector
instance is preseeded into the dispatcher's cache.

What is proven:

* each op registers with the right risk level, needs approval and shows as a
  plain write on the broadcast feed;
* a client role call reads the client and every role first, so an unknown
  client or role is an error and nothing is sent;
* ``keycloak.user.update`` changes only the fields that were sent, and says
  which field Keycloak did not change (the user name when the realm does not
  allow it);
* ``keycloak.user.credential.delete`` removes only the matching credentials and
  lists the type and date of the removed and the remaining ones;
* a recorded credential list that holds ``secretData`` and ``credentialData``
  never reaches a result, an approval card or an audit row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

import meho_backplane.connectors.keycloak  # noqa: F401 -- import for registry side-effects
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write import WRITE_OPS
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-account-e2e"
_KC_HOST = "keycloak-account-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CLIENT_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000005675")
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_MAPPING_PATH = f"{_USER_PATH}/role-mappings/clients/{_CLIENT_UUID}"
_CREDENTIALS_PATH = f"{_USER_PATH}/credentials"

#: Values that sit in the recorded credential list and must never come out.
_SECRET_DATA = '{"value":"hash-of-sentinel-7f3a","salt":"salt-sentinel-9c1d"}'
_CREDENTIAL_DATA = '{"hashIterations":27500,"algorithm":"seed-sentinel-5b2e"}'
_SENTINELS = ("hash-of-sentinel-7f3a", "salt-sentinel-9c1d", "seed-sentinel-5b2e")

_ROLE_ADMIN = {"id": "role-1", "name": "realm-admin", "containerId": _CLIENT_UUID}
_ROLE_VIEW = {"id": "role-2", "name": "view-users", "containerId": _CLIENT_UUID}


def _operator(sub: str = "kc-account-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Account Test Operator",
        email=None,
        raw_jwt="<kc-account-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _creds_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="keycloak",
                host=_KC_HOST,
                port=443,
                fqdn=None,
                secret_ref="keycloak/admin",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={"managed_realm": _REALM, "admin_realm": _ADMIN_REALM},
                fingerprint={"version": "26.3.3"},
                notes="seeded by test_connectors_keycloak_account_writes",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run an op as an already-approved call (the handler path)."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


#: A read that keeps failing: the connector retries a GET four times.
_FAILING_READS = [httpx.Response(500, json={}) for _ in range(4)]


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


def _mount_client_and_roles(
    mock: respx.MockRouter, roles: tuple[dict[str, Any], ...] = (_ROLE_ADMIN,)
) -> None:
    mock.get(f"/admin/realms/{_REALM}/clients").respond(
        200, json=[{"id": _CLIENT_UUID, "clientId": "realm-management"}]
    )
    for role in roles:
        mock.get(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/roles/{role['name']}").respond(
            200, json=role
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_EXPECTED = {
    "keycloak.role_mapping.client.assign": "dangerous",
    "keycloak.role_mapping.client.remove": "dangerous",
    "keycloak.user.update": "caution",
    "keycloak.user.credential.delete": "dangerous",
}


@pytest.mark.parametrize(("op_id", "safety"), sorted(_EXPECTED.items()))
async def test_registers_with_approval_and_plain_write_class(
    kc: KeycloakConnector, op_id: str, safety: str
) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
    assert row.safety_level == safety
    assert row.requires_approval is True
    assert classify_op(op_id) == "write"
    (op,) = [op for op in WRITE_OPS if op.op_id == op_id]
    assert op.group_key == "account_write"


# ---------------------------------------------------------------------------
# Client roles
# ---------------------------------------------------------------------------


async def test_assign_sends_the_role_representations_and_reads_back(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_client_and_roles(mock, (_ROLE_ADMIN, _ROLE_VIEW))
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _USER_UUID, "username": "jane.example"}]
        )
        post = mock.post(_MAPPING_PATH).respond(204)
        mock.get(_MAPPING_PATH).respond(200, json=[_ROLE_ADMIN, _ROLE_VIEW])
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {
                "username": "jane.example",
                "client_id": "realm-management",
                "roles": ["realm-admin", "view-users", "realm-admin"],
            },
        )
    assert result["status"] == "ok", result
    assert result["result"] == {
        "id": _USER_UUID,
        "username": "jane.example",
        "client_id": "realm-management",
        "roles": ["realm-admin", "view-users"],
        "read_back_checked": True,
        "not_applied": [],
    }
    assert json.loads(post.calls.last.request.content) == [_ROLE_ADMIN, _ROLE_VIEW]


async def test_remove_sends_a_delete_with_the_roles_and_checks_they_are_gone(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_client_and_roles(mock)
        delete = mock.delete(_MAPPING_PATH).respond(204)
        mock.get(_MAPPING_PATH).respond(200, json=[_ROLE_VIEW])
        result = await _run(
            "keycloak.role_mapping.client.remove",
            {"id": _USER_UUID, "client_id": "realm-management", "roles": ["realm-admin"]},
        )
    assert result["status"] == "ok", result
    assert result["result"]["not_applied"] == []
    assert json.loads(delete.calls.last.request.content) == [_ROLE_ADMIN]


async def test_remove_reports_a_role_that_is_still_held(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_client_and_roles(mock)
        mock.delete(_MAPPING_PATH).respond(204)
        mock.get(_MAPPING_PATH).respond(200, json=[_ROLE_ADMIN])
        result = await _run(
            "keycloak.role_mapping.client.remove",
            {"id": _USER_UUID, "client_id": "realm-management", "roles": ["realm-admin"]},
        )
    assert result["status"] == "ok", result
    assert result["result"]["not_applied"] == ["realm-admin"]


async def test_a_failed_read_back_is_unknown_not_done(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_client_and_roles(mock)
        post = mock.post(_MAPPING_PATH).respond(204)
        mock.get(_MAPPING_PATH).respond(500, json={"error": "boom"})
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {"id": _USER_UUID, "client_id": "realm-management", "roles": ["realm-admin"]},
        )
    assert post.call_count == 1
    assert result["status"] == "ok", result
    assert result["result"]["read_back_checked"] is False
    assert result["result"]["not_applied"] is None


async def test_unknown_client_is_an_error_and_nothing_is_sent(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(200, json=[])
        post = mock.post(_MAPPING_PATH).respond(204)
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {"id": _USER_UUID, "client_id": "no-such-client", "roles": ["realm-admin"]},
        )
    assert result["status"] != "ok"
    assert "KeycloakClientNotFoundError" in str(result)
    assert not post.called


async def test_one_unknown_role_stops_everything_before_a_write(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_client_and_roles(mock)
        mock.get(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/roles/no-such-role").respond(
            404, json={"error": "Could not find role"}
        )
        post = mock.post(_MAPPING_PATH).respond(204)
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {
                "id": _USER_UUID,
                "client_id": "realm-management",
                "roles": ["realm-admin", "no-such-role"],
            },
        )
    assert result["status"] != "ok"
    assert "KeycloakRoleNotFoundError" in str(result)
    assert not post.called


async def test_a_role_answer_with_another_name_is_an_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(
            200, json=[{"id": _CLIENT_UUID, "clientId": "realm-management"}]
        )
        mock.get(f"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/roles/realm-admin").respond(
            200, json=_ROLE_VIEW
        )
        post = mock.post(_MAPPING_PATH).respond(204)
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {"id": _USER_UUID, "client_id": "realm-management", "roles": ["realm-admin"]},
        )
    assert result["status"] != "ok"
    assert not post.called


async def test_a_role_name_with_a_slash_stays_one_path_segment(kc: KeycloakConnector) -> None:
    role = {"id": "role-3", "name": "a/../b", "containerId": _CLIENT_UUID}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/clients").respond(
            200, json=[{"id": _CLIENT_UUID, "clientId": "realm-management"}]
        )
        read = mock.get(
            url__regex=rf"/admin/realms/{_REALM}/clients/{_CLIENT_UUID}/roles/.*"
        ).respond(200, json=role)
        mock.post(_MAPPING_PATH).respond(204)
        mock.get(_MAPPING_PATH).respond(200, json=[role])
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {"id": _USER_UUID, "client_id": "realm-management", "roles": ["a/../b"]},
        )
    assert result["status"] == "ok", result
    assert read.calls.last.request.url.raw_path.endswith(b"/roles/a%2F%2E%2E%2Fb")


async def test_a_missing_user_on_write_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_client_and_roles(mock)
        mock.post(_MAPPING_PATH).respond(404, json={"error": "User not found"})
        result = await _run(
            "keycloak.role_mapping.client.assign",
            {"id": _USER_UUID, "client_id": "realm-management", "roles": ["realm-admin"]},
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)


@pytest.mark.parametrize(
    "params",
    [
        {"client_id": "realm-management", "roles": ["realm-admin"]},
        {"id": _USER_UUID, "roles": ["realm-admin"]},
        {"id": _USER_UUID, "client_id": "realm-management", "roles": []},
        {"id": _USER_UUID, "client_id": "realm-management", "roles": ["  "]},
    ],
)
async def test_client_role_params_are_checked(
    kc: KeycloakConnector, params: dict[str, Any]
) -> None:
    result = await _run("keycloak.role_mapping.client.assign", params)
    assert result["status"] != "ok"


async def test_assign_waits_for_approval_and_names_the_user_for_the_grant_check(
    kc: KeycloakConnector,
) -> None:
    requester = _operator("kc-account-requester")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _USER_UUID, "username": "jane.example"}]
        )
        post = mock.post(_MAPPING_PATH).respond(204)
        call = await call_operation(
            requester,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.role_mapping.client.assign",
                "target": _TARGET_NAME,
                "params": {
                    "username": "jane.example",
                    "client_id": "realm-management",
                    "roles": ["realm-admin"],
                },
            },
        )
    assert call["status"] == "awaiting_approval", call
    assert not post.called
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, uuid.UUID(call["extras"]["approval_request_id"]))
    assert row is not None
    preview = row.proposed_effect["preview"]
    assert preview["roles"] == ["realm-admin"]
    assert preview["client_id"] == "realm-management"
    assert preview["grant"]["user_id"] == _USER_UUID


# ---------------------------------------------------------------------------
# User details
# ---------------------------------------------------------------------------

_USER_BEFORE = {
    "id": _USER_UUID,
    "username": "jane.example",
    "email": "jane@example.com",
    "firstName": "Jane",
    "lastName": "Example",
    "enabled": True,
    "emailVerified": True,
    "attributes": {"team": ["ops"]},
}


async def test_update_changes_only_the_fields_that_were_sent(kc: KeycloakConnector) -> None:
    after = {**_USER_BEFORE, "email": "jane.new@example.com"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        get = mock.get(_USER_PATH).mock(
            side_effect=[httpx.Response(200, json=_USER_BEFORE), httpx.Response(200, json=after)]
        )
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update", {"id": _USER_UUID, "changes": {"email": "jane.new@example.com"}}
        )
    assert result["status"] == "ok", result
    assert result["result"] == {
        "id": _USER_UUID,
        "username": None,
        "requested_fields": ["email"],
        "applied_fields": ["email"],
        "not_applied": [],
        "unrequested_changes": [],
        "read_back_checked": True,
    }
    assert get.call_count == 2
    assert json.loads(put.calls.last.request.content) == {
        **_USER_BEFORE,
        "email": "jane.new@example.com",
    }


async def test_update_changes_the_user_name_when_the_realm_allows_it(kc: KeycloakConnector) -> None:
    after = {**_USER_BEFORE, "username": "jane.renamed"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[_USER_BEFORE])
        mock.get(_USER_PATH).mock(
            side_effect=[httpx.Response(200, json=_USER_BEFORE), httpx.Response(200, json=after)]
        )
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update",
            {"username": "jane.example", "changes": {"username": "Jane.Renamed"}},
        )
    assert result["status"] == "ok", result
    assert result["result"]["applied_fields"] == ["username"]
    assert result["result"]["not_applied"] == []
    assert json.loads(put.calls.last.request.content)["username"] == "Jane.Renamed"


async def test_update_says_the_user_name_did_not_change_and_names_the_realm_setting(
    kc: KeycloakConnector,
) -> None:
    """Keycloak answers 204 but keeps the old name when editUsernameAllowed is off."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_USER_BEFORE),
                httpx.Response(200, json={**_USER_BEFORE, "firstName": "Janet"}),
            ]
        )
        mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update",
            {"id": _USER_UUID, "changes": {"username": "jane.renamed", "firstName": "Janet"}},
        )
    assert result["status"] == "ok", result
    body = result["result"]
    assert body["applied_fields"] == ["firstName"]
    (miss,) = body["not_applied"]
    assert miss["field"] == "username"
    assert "editUsernameAllowed" in miss["reason"]


async def test_update_compares_user_name_and_email_without_case(kc: KeycloakConnector) -> None:
    after = {**_USER_BEFORE, "email": "jane.upper@example.com"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).mock(
            side_effect=[httpx.Response(200, json=_USER_BEFORE), httpx.Response(200, json=after)]
        )
        mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update",
            {"id": _USER_UUID, "changes": {"email": "Jane.Upper@Example.com"}},
        )
    assert result["result"]["applied_fields"] == ["email"]


async def test_update_reports_a_field_that_changed_without_being_asked(
    kc: KeycloakConnector,
) -> None:
    after = {**_USER_BEFORE, "email": "jane.new@example.com", "enabled": False}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).mock(
            side_effect=[httpx.Response(200, json=_USER_BEFORE), httpx.Response(200, json=after)]
        )
        mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update", {"id": _USER_UUID, "changes": {"email": "jane.new@example.com"}}
        )
    assert result["result"]["unrequested_changes"] == ["enabled"]


async def test_update_with_a_failed_read_back_is_unknown_not_done(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).mock(
            side_effect=[httpx.Response(200, json=_USER_BEFORE), *_FAILING_READS]
        )
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update", {"id": _USER_UUID, "changes": {"email": "a@b.c"}}
        )
    assert put.call_count == 1
    assert result["status"] == "ok", result
    assert result["result"]["read_back_checked"] is False
    assert result["result"]["applied_fields"] is None
    assert result["result"]["not_applied"] is None


async def test_update_of_a_missing_user_is_not_found_and_nothing_is_written(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).respond(404, json={"error": "User not found"})
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(
            "keycloak.user.update", {"id": _USER_UUID, "changes": {"email": "a@b.c"}}
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


@pytest.mark.parametrize(
    "params",
    [
        {"id": _USER_UUID},
        {"id": _USER_UUID, "changes": {}},
        {"id": _USER_UUID, "changes": {"enabled": False}},
        {"id": _USER_UUID, "changes": {"email": None}},
        {"id": _USER_UUID, "changes": {"username": "  "}},
        {"changes": {"email": "a@b.c"}},
    ],
)
async def test_update_params_are_checked(kc: KeycloakConnector, params: dict[str, Any]) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        put = mock.put(_USER_PATH).respond(204)
        result = await _run("keycloak.user.update", params)
    assert result["status"] != "ok"
    assert not put.called


# ---------------------------------------------------------------------------
# One stored credential
# ---------------------------------------------------------------------------

_CREDENTIALS = [
    {
        "id": "cred-pw",
        "type": "password",
        "userLabel": "My password",
        "createdDate": 1700000000000,
        "secretData": _SECRET_DATA,
        "credentialData": _CREDENTIAL_DATA,
    },
    {
        "id": "cred-otp-1",
        "type": "otp",
        "userLabel": "Old phone",
        "createdDate": 1700000001000,
        "secretData": _SECRET_DATA,
        "credentialData": _CREDENTIAL_DATA,
    },
    {
        "id": "cred-otp-2",
        "type": "otp",
        "createdDate": 1700000002000,
        "secretData": _SECRET_DATA,
        "credentialData": _CREDENTIAL_DATA,
    },
    {
        "id": "cred-key",
        "type": "webauthn-passwordless",
        "userLabel": "Hardware key",
        "createdDate": 1700000003000,
        "secretData": _SECRET_DATA,
        "credentialData": _CREDENTIAL_DATA,
    },
]


def _clean(*ids: str) -> list[dict[str, Any]]:
    keep = ("id", "type", "userLabel", "createdDate")
    return [{k: c[k] for k in keep if k in c} for c in _CREDENTIALS if c["id"] in ids]


def _mount_user_lookup(mock: respx.MockRouter) -> None:
    mock.get(f"/admin/realms/{_REALM}/users").respond(
        200, json=[{"id": _USER_UUID, "username": "jane.example"}]
    )


async def test_delete_by_type_removes_every_match_and_lists_removed_and_remaining(
    kc: KeycloakConnector,
) -> None:
    remaining = [c for c in _CREDENTIALS if c["type"] != "otp"]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_user_lookup(mock)
        mock.get(_CREDENTIALS_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_CREDENTIALS),
                httpx.Response(200, json=remaining),
            ]
        )
        one = mock.delete(f"{_CREDENTIALS_PATH}/cred-otp-1").respond(204)
        two = mock.delete(f"{_CREDENTIALS_PATH}/cred-otp-2").respond(204)
        other = mock.delete(url__regex=r".*/credentials/cred-(pw|key)$").respond(204)
        result = await _run(
            "keycloak.user.credential.delete",
            {"username": "jane.example", "credential_type": "otp"},
        )
    assert result["status"] == "ok", result
    assert result["result"] == {
        "id": _USER_UUID,
        "username": "jane.example",
        "removed": _clean("cred-otp-1", "cred-otp-2"),
        "remaining": _clean("cred-pw", "cred-key"),
        "read_back_checked": True,
    }
    assert one.call_count == 1
    assert two.call_count == 1
    assert not other.called


async def test_delete_by_id_removes_only_that_credential(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_CREDENTIALS),
                httpx.Response(200, json=[c for c in _CREDENTIALS if c["id"] != "cred-key"]),
            ]
        )
        key = mock.delete(f"{_CREDENTIALS_PATH}/cred-key").respond(204)
        other = mock.delete(url__regex=r".*/credentials/cred-(pw|otp-1|otp-2)$").respond(204)
        result = await _run(
            "keycloak.user.credential.delete", {"id": _USER_UUID, "credential_id": "cred-key"}
        )
    assert result["status"] == "ok", result
    assert result["result"]["removed"] == _clean("cred-key")
    assert [c["id"] for c in result["result"]["remaining"]] == [
        "cred-pw",
        "cred-otp-1",
        "cred-otp-2",
    ]
    assert key.call_count == 1
    assert not other.called


async def test_delete_with_no_match_names_the_types_the_user_has_and_deletes_nothing(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).respond(200, json=_CREDENTIALS)
        deletes = mock.delete(url__regex=r".*/credentials/.*").respond(204)
        result = await _run(
            "keycloak.user.credential.delete", {"id": _USER_UUID, "credential_type": "recovery"}
        )
    assert result["status"] != "ok"
    text = str(result)
    assert "KeycloakCredentialNotFoundError" in text
    for kind in ("otp", "password", "webauthn-passwordless"):
        assert kind in text
    assert not deletes.called
    for sentinel in _SENTINELS:
        assert sentinel not in text


@pytest.mark.parametrize(
    "params",
    [
        {"id": _USER_UUID},
        {"id": _USER_UUID, "credential_id": "cred-key", "credential_type": "otp"},
        {"credential_type": "otp"},
    ],
)
async def test_delete_params_are_checked(kc: KeycloakConnector, params: dict[str, Any]) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).respond(200, json=_CREDENTIALS)
        deletes = mock.delete(url__regex=r".*/credentials/.*").respond(204)
        result = await _run("keycloak.user.credential.delete", params)
    assert result["status"] != "ok"
    assert not deletes.called


async def test_delete_of_an_unreadable_list_is_an_error_not_an_empty_list(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).respond(200, json={"error": "odd body"})
        deletes = mock.delete(url__regex=r".*/credentials/.*").respond(204)
        result = await _run(
            "keycloak.user.credential.delete", {"id": _USER_UUID, "credential_type": "otp"}
        )
    assert result["status"] != "ok"
    assert not deletes.called


async def test_delete_for_a_missing_user_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).respond(404, json={"error": "User not found"})
        result = await _run(
            "keycloak.user.credential.delete", {"id": _USER_UUID, "credential_type": "otp"}
        )
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)


async def test_a_failed_second_delete_names_the_credential_already_removed(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).respond(200, json=_CREDENTIALS)
        mock.delete(f"{_CREDENTIALS_PATH}/cred-otp-1").respond(204)
        mock.delete(f"{_CREDENTIALS_PATH}/cred-otp-2").respond(500, json={"error": "boom"})
        result = await _run(
            "keycloak.user.credential.delete", {"id": _USER_UUID, "credential_type": "otp"}
        )
    assert result["status"] != "ok"
    text = str(result)
    assert "KeycloakCredentialDeleteError" in text
    assert "cred-otp-1" in text


async def test_a_failed_read_back_after_the_deletes_is_unknown_not_empty(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_CREDENTIALS_PATH).mock(
            side_effect=[httpx.Response(200, json=_CREDENTIALS), *_FAILING_READS]
        )
        mock.delete(f"{_CREDENTIALS_PATH}/cred-key").respond(204)
        result = await _run(
            "keycloak.user.credential.delete", {"id": _USER_UUID, "credential_id": "cred-key"}
        )
    assert result["status"] == "ok", result
    assert result["result"]["read_back_checked"] is False
    assert result["result"]["remaining"] is None
    assert result["result"]["removed"] == _clean("cred-key")


async def test_no_secret_data_reaches_a_result_a_card_or_an_audit_row(
    kc: KeycloakConnector,
) -> None:
    """A recorded list with secretData and credentialData never comes out."""
    requester = _operator("kc-account-requester")
    # A MEHO-SA-Operator may approve another person's high-risk change.
    approver = _operator("kc-account-approver").model_copy(update={"self_approver": True})
    remaining = [c for c in _CREDENTIALS if c["type"] != "otp"]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_user_lookup(mock)
        mock.get(_CREDENTIALS_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_CREDENTIALS),  # preview
                httpx.Response(200, json=_CREDENTIALS),  # run
                httpx.Response(200, json=remaining),  # read back
            ]
        )
        mock.delete(url__regex=r".*/credentials/cred-otp-\d$").respond(204)
        call = await call_operation(
            requester,
            {
                "connector_id": _CONNECTOR_ID,
                "op_id": "keycloak.user.credential.delete",
                "target": _TARGET_NAME,
                "params": {"username": "jane.example", "credential_type": "otp"},
            },
        )
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status == "ok", resumed

    async with get_sessionmaker()() as session:
        approvals = (await session.execute(select(ApprovalRequest))).scalars().all()
        audits = (await session.execute(select(AuditLog))).scalars().all()
    assert audits, "the dispatch wrote no audit rows"

    def _dump(rows: Any, model: Any) -> str:
        return json.dumps(
            [{c.name: getattr(r, c.name) for c in model.__table__.columns} for r in rows],
            default=str,
        )

    card = next(r for r in approvals if r.id == request_id).proposed_effect["preview"]
    assert card["will_remove"] == _clean("cred-otp-1", "cred-otp-2")
    assert card["will_keep"] == _clean("cred-pw", "cred-key")
    blobs = {
        "the result": resumed.model_dump_json(),
        "the approval rows": _dump(approvals, ApprovalRequest),
        "the audit rows": _dump(audits, AuditLog),
    }
    for label, blob in blobs.items():
        for sentinel in _SENTINELS:
            assert sentinel not in blob, f"secret data appears in {label}"
        assert "secretData" not in blob, f"a secret field name appears in {label}"
        assert "credentialData" not in blob, f"a secret field name appears in {label}"
