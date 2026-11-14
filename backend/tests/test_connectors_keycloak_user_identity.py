# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak user writes show the stored user name and refuse an id and name that differ.

Drives the ops through the real dispatch stack against a respx-mocked
Keycloak Admin REST API. No running Keycloak and no Vault.

What is proven, for every user write that takes an ``id`` and a ``username``:

* an ``id`` and a ``username`` of different users are refused before the
  request waits for approval, and no approval request is stored;
* a user that is renamed between the request and the run is refused;
* with only an ``id`` or only a ``username``, the call works as before, and
  the approval card shows the username stored in Keycloak;
* when the user cannot be read, the card does not show the sent name as the
  user's name.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import approved_preview_var
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-identity-e2e"
_KC_HOST = "keycloak-identity-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000006357")
_DISABLE = "keycloak.user.disable"


def _user(enabled: bool) -> dict[str, Any]:
    """A made-up user as Keycloak returns it."""
    return {
        "id": _USER_UUID,
        "username": "event-guest",
        "email": "guest@example.invalid",
        "firstName": "Event",
        "enabled": enabled,
        "attributes": {"seat": ["12"]},
        "requiredActions": ["VERIFY_EMAIL"],
    }


def _operator(
    sub: str = "kc-identity-requester", role: TenantRole = TenantRole.OPERATOR
) -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Identity Test Operator",
        email=None,
        raw_jwt="<kc-identity-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
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
                notes="seeded by test_connectors_keycloak_user_identity",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run the op as an already-approved call (the handler path)."""
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


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_GROUP_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_USERS_PATH = f"/admin/realms/{_REALM}/users"
_USER_OP_PARAMS: dict[str, dict[str, Any]] = {
    "keycloak.user.disable": {},
    "keycloak.user.enable": {},
    "keycloak.user.logout": {},
    "keycloak.user.reset_password": {"password_secret_ref": "kv/made-up/password"},
    "keycloak.role_mapping.assign": {"roles": ["viewer"]},
}
_MEMBER_OPS = ("keycloak.group.member.add", "keycloak.group.member.remove")


def _call_args(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _TARGET_NAME,
        "params": params,
    }


async def _stored_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _mount_user(mock: respx.MockRouter, user: dict[str, Any]) -> respx.Route:
    return mock.get(_USER_PATH).respond(200, json=user)


async def _approve_and_resume(request_id: uuid.UUID) -> Any:
    approver = _operator("kc-identity-approver", TenantRole.TENANT_ADMIN)
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


# ---------------------------------------------------------------------------
# An id and a username of different users
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", sorted(_USER_OP_PARAMS))
async def test_id_and_username_of_different_users_is_refused_before_approval(
    kc: KeycloakConnector, op_id: str
) -> None:
    params = {"id": _USER_UUID, "username": "someone-else", **_USER_OP_PARAMS[op_id]}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_user(mock, _user(True))
        writes = mock.route(method__in=["PUT", "POST", "DELETE"]).respond(204)
        call = await call_operation(_operator(), _call_args(op_id, params))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "belong to different users" in call["error"]
    assert not writes.called
    assert await _stored_requests() == []


@pytest.mark.parametrize("op_id", _MEMBER_OPS)
async def test_member_ops_refuse_a_user_id_and_a_username_of_different_users(
    kc: KeycloakConnector, op_id: str
) -> None:
    params = {"group_id": _GROUP_UUID, "user_id": _USER_UUID, "username": "someone-else"}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_user(mock, _user(True))
        writes = mock.route(method__in=["PUT", "POST", "DELETE"]).respond(204)
        call = await call_operation(_operator(), _call_args(op_id, params))
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "'user_id' and 'username' belong to different users" in call["error"]
    assert not writes.called
    assert await _stored_requests() == []


async def test_user_delete_refuses_different_users_before_approval(
    kc: KeycloakConnector,
) -> None:
    args = _call_args("keycloak.user.delete", {"id": _USER_UUID, "username": "someone-else"})
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        # The preview refuses a mismatch too, so take a valid hash while the names match.
        user_read = _mount_user(mock, {**_user(True), "username": "someone-else"})
        mock.get(f"{_USER_PATH}/groups").respond(200, json=[])
        deletes = mock.delete(url__regex=r".*").respond(204)
        preview = await preview_operation(_operator(), args)
        assert preview["status"] == "ok", preview
        user_read.respond(200, json=_user(True))
        call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "belong to different users" in call["error"]
    assert not deletes.called
    assert await _stored_requests() == []


async def _park_user_delete(params: dict[str, Any]) -> uuid.UUID:
    """Preview a user delete, then send it with the hash: it waits for approval."""
    args = _call_args("keycloak.user.delete", params)
    preview = await preview_operation(_operator(), args)
    assert preview["status"] == "ok", preview
    call = await call_operation(_operator(), {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "awaiting_approval", call
    return uuid.UUID(call["extras"]["approval_request_id"])


async def _delete_card(request_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        card: dict[str, Any] = row.proposed_effect["preview"]["blast_radius"]["object"]
    return card


async def test_user_delete_of_an_absent_user_is_refused_when_the_name_exists_at_run_time(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(_USERS_PATH).respond(200, json=[])
        mock.get(_USER_PATH).respond(200, json=_user(True))
        mock.get(f"{_USER_PATH}/groups").respond(200, json=[])
        deletes = mock.delete(url__regex=r".*").respond(204)
        request_id = await _park_user_delete({"username": "event-guest"})
        card = await _delete_card(request_id)
        # A user with that name is made while the request waits.
        lookup.respond(200, json=[_user(True)])
        resumed = await _approve_and_resume(request_id)
    assert "username" not in card
    assert card["requested_username"] == "event-guest"
    assert card["present"] is False
    assert resumed.status != "ok"
    assert not deletes.called


async def test_user_delete_handler_refuses_a_card_that_said_absent(
    kc: KeycloakConnector,
) -> None:
    card = {"blast_radius": {"object": {"realm": _REALM, "id": None, "present": False}}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USERS_PATH).respond(200, json=[_user(True)])
        _mount_user(mock, _user(True))
        deletes = mock.delete(url__regex=r".*").respond(204)
        token = approved_preview_var.set(("keycloak.user.delete", card))
        try:
            result = await _run("keycloak.user.delete", {"username": "event-guest"})
        finally:
            approved_preview_var.reset(token)
    assert result["status"] != "ok"
    assert "the approval card said the user does not exist" in str(result)
    assert not deletes.called


async def test_user_delete_does_not_delete_a_new_user_with_the_same_name(
    kc: KeycloakConnector,
) -> None:
    new_uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(_USERS_PATH).respond(200, json=[_user(True)])
        old_read = _mount_user(mock, _user(True))
        mock.get(f"{_USER_PATH}/groups").respond(200, json=[])
        mock.get(f"{_USERS_PATH}/{new_uuid}").respond(200, json={**_user(True), "id": new_uuid})
        deletes = mock.delete(url__regex=r".*").respond(204)
        request_id = await _park_user_delete({"username": "event-guest"})
        # The user is deleted and made again with the same name: a new id.
        old_read.respond(404)
        lookup.respond(200, json=[{**_user(True), "id": new_uuid}])
        resumed = await _approve_and_resume(request_id)
    # MEHO either refuses the run or finds nothing to delete at the old id.
    assert resumed.status != "ok" or resumed.result["already_absent"] is True, resumed
    assert not deletes.called


async def test_user_delete_deletes_the_user_on_the_card(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        lookup = mock.get(_USERS_PATH).respond(200, json=[_user(True)])
        user_read = _mount_user(mock, _user(True))
        mock.get(f"{_USER_PATH}/groups").respond(200, json=[])
        delete = mock.delete(_USER_PATH).mock(
            side_effect=lambda _request: (user_read.respond(404), httpx.Response(204))[1]
        )
        request_id = await _park_user_delete({"username": "event-guest"})
        card = await _delete_card(request_id)
        lookup.reset()
        resumed = await _approve_and_resume(request_id)
    assert card["id"] == _USER_UUID
    assert card["username"] == "event-guest"
    assert resumed.status == "ok", resumed
    assert resumed.result == {
        "realm": _REALM,
        "id": _USER_UUID,
        "username": "event-guest",
        "deleted": True,
        "already_absent": False,
    }
    assert delete.call_count == 1


async def test_user_delete_handler_uses_the_id_on_the_card(kc: KeycloakConnector) -> None:
    other_uuid = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    shown = {"realm": _REALM, "id": _USER_UUID, "username": "event-guest", "present": True}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        # A new lookup of the name would find another user.
        lookup = mock.get(_USERS_PATH).respond(200, json=[{**_user(True), "id": other_uuid}])
        user_read = _mount_user(mock, _user(True))
        other_delete = mock.delete(f"{_USERS_PATH}/{other_uuid}").respond(204)
        delete = mock.delete(_USER_PATH).mock(
            side_effect=lambda _request: (user_read.respond(404), httpx.Response(204))[1]
        )
        token = approved_preview_var.set(
            ("keycloak.user.delete", {"blast_radius": {"object": shown}})
        )
        try:
            result = await _run("keycloak.user.delete", {"username": "event-guest"})
        finally:
            approved_preview_var.reset(token)
    assert result["status"] == "ok", result
    assert result["result"]["id"] == _USER_UUID
    assert result["result"]["deleted"] is True
    assert delete.call_count == 1
    assert not other_delete.called
    assert not lookup.called


async def test_user_delete_of_a_renamed_user_is_refused_when_it_runs(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        user_read = _mount_user(mock, _user(True))
        mock.get(f"{_USER_PATH}/groups").respond(200, json=[])
        deletes = mock.delete(url__regex=r".*").respond(204)
        request_id = await _park_user_delete({"id": _USER_UUID})
        user_read.respond(200, json={**_user(True), "username": "renamed-guest"})
        resumed = await _approve_and_resume(request_id)
    assert resumed.status != "ok"
    assert "is not the name on the approval card" in str(resumed.model_dump(mode="json"))
    assert not deletes.called


async def test_a_username_in_another_case_is_the_same_user(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USER_PATH).mock(
            side_effect=[
                httpx.Response(200, json=_user(True)),
                httpx.Response(200, json=_user(True)),
                httpx.Response(200, json=_user(False)),
            ]
        )
        put = mock.put(_USER_PATH).respond(204)
        result = await _run(_DISABLE, {"id": _USER_UUID, "username": "Event-Guest"})
    assert result["status"] == "ok", result
    assert result["result"]["username"] == "event-guest"
    assert put.call_count == 1


# ---------------------------------------------------------------------------
# The user is renamed between the request and the run
# ---------------------------------------------------------------------------


_RENAME_CASES = [
    *(
        (op_id, {"id": _USER_UUID, "username": "event-guest", **extra})
        for op_id, extra in sorted(_USER_OP_PARAMS.items())
    ),
    *(
        (op_id, {"group_id": _GROUP_UUID, "user_id": _USER_UUID, "username": "event-guest"})
        for op_id in _MEMBER_OPS
    ),
]


@pytest.mark.parametrize(("op_id", "params"), _RENAME_CASES, ids=[c[0] for c in _RENAME_CASES])
async def test_a_user_renamed_after_the_request_is_refused_when_it_runs(
    kc: KeycloakConnector, op_id: str, params: dict[str, Any]
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        user_read = _mount_user(mock, _user(True))
        mock.get(f"/admin/realms/{_REALM}/groups/{_GROUP_UUID}").respond(
            200, json={"id": _GROUP_UUID, "name": "event-guests", "path": "/event-guests"}
        )
        writes = mock.route(method__in=["PUT", "POST", "DELETE"]).respond(204)
        call = await call_operation(_operator(), _call_args(op_id, params))
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        user_read.respond(200, json={**_user(True), "username": "renamed-guest"})
        resumed = await _approve_and_resume(request_id)
    assert resumed.status != "ok"
    # Some ops withhold the error text, so the check is on the error class.
    assert "ValueError" in str(resumed.model_dump(mode="json"))
    assert not writes.called


async def test_a_username_request_is_refused_when_the_user_is_renamed(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USERS_PATH).respond(200, json=[_user(True)])
        user_read = _mount_user(mock, _user(True))
        put = mock.put(_USER_PATH).respond(204)
        call = await call_operation(_operator(), _call_args(_DISABLE, {"username": "event-guest"}))
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        user_read.respond(200, json={**_user(True), "username": "renamed-guest"})
        resumed = await _approve_and_resume(request_id)
    assert resumed.status != "ok"
    assert not put.called


# ---------------------------------------------------------------------------
# What the card shows
# ---------------------------------------------------------------------------


async def _card(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    call = await call_operation(_operator(), _call_args(op_id, params))
    assert call["status"] == "awaiting_approval", call
    request_id = uuid.UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        card: dict[str, Any] = row.proposed_effect["preview"]
    return card


@pytest.mark.parametrize(
    "op_id", [_DISABLE, "keycloak.user.logout", "keycloak.role_mapping.assign"]
)
async def test_card_shows_the_stored_name_for_an_id(kc: KeycloakConnector, op_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_user(mock, _user(True))
        card = await _card(op_id, {"id": _USER_UUID, **_USER_OP_PARAMS.get(op_id, {})})
    assert card["id"] == _USER_UUID
    assert card["username"] == "event-guest"
    assert card["user_available"] is True


@pytest.mark.parametrize("op_id", [_DISABLE, "keycloak.user.logout"])
async def test_card_shows_the_stored_name_for_a_username(kc: KeycloakConnector, op_id: str) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USERS_PATH).respond(200, json=[_user(True)])
        _mount_user(mock, _user(True))
        card = await _card(op_id, {"username": "EVENT-GUEST"})
    assert card["id"] == _USER_UUID
    assert card["username"] == "event-guest"
    assert "requested_username" not in card


async def test_card_does_not_show_the_sent_name_when_the_user_cannot_be_read(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(_USERS_PATH).respond(500)
        card = await _card(_DISABLE, {"username": "event-guest"})
    assert "username" not in card
    assert card["requested_username"] == "event-guest"
    assert card["user_available"] is False


async def test_card_of_a_member_change_shows_the_stored_name(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        _mount_user(mock, _user(True))
        card = await _card(
            "keycloak.group.member.remove", {"group_id": _GROUP_UUID, "user_id": _USER_UUID}
        )
    assert card["user_id"] == _USER_UUID
    assert card["username"] == "event-guest"
