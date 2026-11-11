# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One-time code set-up: ``keycloak.user.required_actions.update`` and ``keycloak.user.otp_status``.

Drives both ops through the real dispatch stack against a respx-mocked
Keycloak 26 Admin REST API (made-up names and ids, answers in the shape
Keycloak 26 returns). No running Keycloak and no Vault.

What is proven:

* the write adds ``CONFIGURE_TOTP`` and keeps the other required actions;
* the write removes a named action the same way, and writes nothing when
  the list would not change;
* an unknown or disabled name is refused, the error lists the allowed names,
  and nothing is written;
* the write waits for approval, and the approval card shows the user, the
  realm and the change;
* the read shows each user's required actions and whether a one-time code
  is set up, shows "unknown" (null) when credentials cannot be read, says
  when the list was cut, and never returns a credential value.
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
from meho_backplane.connectors.keycloak.ops_user_required_actions import (
    USER_REQUIRED_ACTION_OPS,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
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

_OP_ID = "keycloak.user.required_actions.update"
_OTP_OP_ID = "keycloak.user.otp_status"
_CONNECTOR_ID = "keycloak-admin-26.x"
_TARGET_NAME = "kc-otp-e2e"
_KC_HOST = "keycloak-otp-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_REALM = "meho"
_ADMIN_REALM = "master"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_ACTIONS_PATH = f"/admin/realms/{_REALM}/authentication/required-actions"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003852")


def _operator(sub: str = "kc-otp-requester") -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak OTP Test Operator",
        email=None,
        raw_jwt="<kc-otp-raw-jwt>",
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
                notes="seeded by test_connectors_keycloak_user_otp",
            )
        )
        await session.commit()
    connector = KeycloakConnector(credentials_loader=_creds_loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    yield connector
    await connector.aclose()


def _mount_token(mock: respx.MockRouter) -> None:
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )


_ALLOWED = [
    {"alias": "CONFIGURE_TOTP", "name": "Configure OTP", "enabled": True, "defaultAction": False},
    {"alias": "VERIFY_EMAIL", "name": "Verify Email", "enabled": True, "defaultAction": False},
    {"alias": "UPDATE_PASSWORD", "name": "Update Password", "enabled": True},
    {"alias": "TERMS_AND_CONDITIONS", "name": "Terms", "enabled": False},
]


def _user(actions: list[str]) -> dict[str, Any]:
    return {
        "id": _USER_UUID,
        "username": "event-guest",
        "enabled": True,
        "emailVerified": True,
        "firstName": "Eve",
        "attributes": {"team": ["ops"]},
        "requiredActions": actions,
    }


def _mount(
    mock: respx.MockRouter, actions: list[str], *, after: list[str] | None = None
) -> tuple[respx.Route, respx.Route]:
    """Mount token, lookup, the user GET (before, then after the PUT) and the PUT."""
    mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token", "expires_in": 300}
    )
    mock.get(f"/admin/realms/{_REALM}/users").respond(
        200, json=[{"id": _USER_UUID, "username": "event-guest"}]
    )
    mock.get(_ACTIONS_PATH).respond(200, json=_ALLOWED)
    put = mock.put(_USER_PATH).respond(204)

    def _answer(_request: httpx.Request) -> httpx.Response:
        # The read-back after the write sees the new list.
        current = after if after is not None and put.called else actions
        return httpx.Response(200, json=_user(current))

    get = mock.get(_USER_PATH).mock(side_effect=_answer)
    return get, put


async def _target() -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, _TARGET_NAME)


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_ops_register_with_the_right_risk_level(kc: KeycloakConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = {
            row.op_id: row
            for row in (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_([_OP_ID, _OTP_OP_ID])
                    )
                )
            ).scalars()
        }
    assert rows[_OP_ID].safety_level == "caution"
    assert rows[_OP_ID].requires_approval is True
    assert rows[_OTP_OP_ID].safety_level == "safe"
    assert rows[_OTP_OP_ID].requires_approval is False
    assert {op.op_id for op in USER_REQUIRED_ACTION_OPS} == {_OP_ID, _OTP_OP_ID}


def test_the_write_classifies_as_write_and_the_read_does_not() -> None:
    assert classify_op(_OP_ID) == "write"
    assert classify_op(_OTP_OP_ID) != "write"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"username": "event-guest"},
        {"add": ["CONFIGURE_TOTP"]},
        {"username": "event-guest", "add": []},
    ],
)
async def test_missing_user_or_change_is_invalid_params(
    kc: KeycloakConnector, params: dict[str, Any]
) -> None:
    result = await _run(_OP_ID, params)
    assert result["status"] != "ok"


async def test_same_name_in_add_and_remove_is_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        result = await _run(
            _OP_ID, {"username": "event-guest", "add": ["VERIFY_EMAIL"], "remove": ["VERIFY_EMAIL"]}
        )
    assert result["status"] != "ok"
    assert "both add and remove" in str(result)
    assert not put.called


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


async def test_add_keeps_the_other_required_actions(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, ["VERIFY_EMAIL"], after=["VERIFY_EMAIL", "CONFIGURE_TOTP"])
        result = await _run(_OP_ID, {"username": "event-guest", "add": ["CONFIGURE_TOTP"]})
    assert result["status"] == "ok", result
    assert put.call_count == 1
    body = json.loads(put.calls.last.request.content)
    assert body["requiredActions"] == ["VERIFY_EMAIL", "CONFIGURE_TOTP"]
    # The rest of the user is written back as it was read.
    assert body["attributes"] == {"team": ["ops"]}
    assert body["firstName"] == "Eve"
    assert result["result"] == {
        "id": _USER_UUID,
        "username": "event-guest",
        "realm": _REALM,
        "changed": True,
        "added": ["CONFIGURE_TOTP"],
        "removed": [],
        "required_actions": ["VERIFY_EMAIL", "CONFIGURE_TOTP"],
        "unrequested_changes": [],
        "read_back_checked": True,
    }


async def test_remove_drops_only_the_named_action(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, ["CONFIGURE_TOTP", "VERIFY_EMAIL"], after=["VERIFY_EMAIL"])
        result = await _run(_OP_ID, {"id": _USER_UUID, "remove": ["CONFIGURE_TOTP"]})
    assert result["status"] == "ok", result
    assert json.loads(put.calls.last.request.content)["requiredActions"] == ["VERIFY_EMAIL"]
    assert result["result"]["removed"] == ["CONFIGURE_TOTP"]
    assert result["result"]["required_actions"] == ["VERIFY_EMAIL"]


async def test_add_and_remove_in_one_call(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, ["UPDATE_PASSWORD"], after=["CONFIGURE_TOTP"])
        result = await _run(
            _OP_ID, {"id": _USER_UUID, "add": ["CONFIGURE_TOTP"], "remove": ["UPDATE_PASSWORD"]}
        )
    assert result["status"] == "ok", result
    assert json.loads(put.calls.last.request.content)["requiredActions"] == ["CONFIGURE_TOTP"]


async def test_nothing_to_change_writes_nothing(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, ["CONFIGURE_TOTP"])
        result = await _run(
            _OP_ID,
            {"id": _USER_UUID, "add": ["CONFIGURE_TOTP"], "remove": ["UPDATE_PASSWORD"]},
        )
    assert result["status"] == "ok", result
    assert not put.called
    assert result["result"]["changed"] is False
    assert result["result"]["added"] == []
    assert result["result"]["removed"] == []


@pytest.mark.parametrize("name", ["CONFIGURE_TOTP_TYPO", "TERMS_AND_CONDITIONS", "configure_totp"])
async def test_unknown_or_disabled_name_is_refused_and_lists_allowed_names(
    kc: KeycloakConnector, name: str
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        result = await _run(_OP_ID, {"id": _USER_UUID, "add": [name]})
    assert result["status"] != "ok"
    text = str(result)
    assert name in text
    assert "['CONFIGURE_TOTP', 'UPDATE_PASSWORD', 'VERIFY_EMAIL']" in text
    assert not put.called


async def test_unknown_username_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        result = await _run(_OP_ID, {"username": "nobody", "add": ["CONFIGURE_TOTP"]})
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


async def test_unknown_id_is_not_found(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        mock.get(_USER_PATH).respond(404, json={"error": "User not found"})
        result = await _run(_OP_ID, {"id": _USER_UUID, "add": ["CONFIGURE_TOTP"]})
    assert result["status"] != "ok"
    assert "KeycloakUserNotFoundError" in str(result)
    assert not put.called


async def test_user_without_a_required_actions_list_is_an_error_not_an_empty_list(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        mock.get(_USER_PATH).respond(200, json={"id": _USER_UUID, "username": "event-guest"})
        result = await _run(_OP_ID, {"id": _USER_UUID, "add": ["CONFIGURE_TOTP"]})
    assert result["status"] != "ok"
    assert not put.called


async def test_the_write_uses_the_read_it_built_the_list_from(kc: KeycloakConnector) -> None:
    # Each read of the user answers a different list and first name, as if
    # someone changed the user between reads. The written body must be the
    # read the new list was built from, so no action of that read is lost.
    reads = [["VERIFY_EMAIL"], ["UPDATE_PASSWORD"], ["UPDATE_PASSWORD", "CONFIGURE_TOTP"]]
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        get, put = _mount(mock, [])
        puts_seen: list[int] = []

        def _answer(_request: httpx.Request) -> httpx.Response:
            index = min(len(puts_seen), len(reads) - 1)
            puts_seen.append(put.call_count)
            return httpx.Response(200, json={**_user(reads[index]), "firstName": f"read-{index}"})

        get.mock(side_effect=_answer)
        result = await _run(_OP_ID, {"id": _USER_UUID, "add": ["CONFIGURE_TOTP"]})
    assert result["status"] == "ok", result
    # One read before the write, one read-back after it.
    assert puts_seen == [0, 1]
    body = json.loads(put.calls.last.request.content)
    assert body["firstName"] == "read-0"
    assert body["requiredActions"] == ["VERIFY_EMAIL", "CONFIGURE_TOTP"]
    assert result["result"]["required_actions"] == body["requiredActions"]


async def test_a_failed_write_is_an_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        put.respond(500, json={"error": "boom"})
        result = await _run(_OP_ID, {"id": _USER_UUID, "add": ["CONFIGURE_TOTP"]})
    assert result["status"] != "ok"


# ---------------------------------------------------------------------------
# The approval gate and card
# ---------------------------------------------------------------------------


async def test_write_waits_for_approval_and_the_card_shows_the_change(
    kc: KeycloakConnector,
) -> None:
    requester, approver = _operator("kc-otp-requester"), _operator("kc-otp-approver")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": {"username": "event-guest", "add": ["CONFIGURE_TOTP"]},
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, ["VERIFY_EMAIL"], after=["VERIFY_EMAIL", "CONFIGURE_TOTP"])
        call = await call_operation(requester, args)
        assert call["status"] == "awaiting_approval", call
        assert not put.called
        request_id = uuid.UUID(call["extras"]["approval_request_id"])

        async with get_sessionmaker()() as session:
            pending = await session.get(ApprovalRequest, request_id)
            assert pending is not None
            preview = pending.proposed_effect["preview"]
        assert preview["username"] == "event-guest"
        assert preview["id"] == _USER_UUID
        assert preview["realm"] == _REALM
        assert preview["add"] == ["CONFIGURE_TOTP"]
        assert preview["remove"] == []
        assert preview["current_required_actions"] == ["VERIFY_EMAIL"]
        assert preview["resulting_required_actions"] == ["VERIFY_EMAIL", "CONFIGURE_TOTP"]

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
    assert put.call_count == 1


_OTHER_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


async def _park(params: dict[str, Any]) -> uuid.UUID:
    call = await call_operation(
        _operator("kc-otp-requester"),
        {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": _TARGET_NAME, "params": params},
    )
    assert call["status"] == "awaiting_approval", call
    return uuid.UUID(call["extras"]["approval_request_id"])


async def _approve_and_resume(request_id: uuid.UUID) -> Any:
    approver = _operator("kc-otp-approver")
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


async def test_approved_run_acts_on_the_user_id_on_the_card(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, ["VERIFY_EMAIL"], after=["VERIFY_EMAIL", "CONFIGURE_TOTP"])
        request_id = await _park({"username": "event-guest", "add": ["CONFIGURE_TOTP"]})
        # While the request waits, the username moves to another account.
        mock.get(f"/admin/realms/{_REALM}/users").respond(
            200, json=[{"id": _OTHER_UUID, "username": "event-guest"}]
        )
        other_path = f"/admin/realms/{_REALM}/users/{_OTHER_UUID}"
        other_get = mock.get(other_path).respond(200, json={**_user([]), "id": _OTHER_UUID})
        other_put = mock.put(other_path).respond(204)
        resumed = await _approve_and_resume(request_id)
    assert resumed.status == "ok", resumed
    assert put.call_count == 1
    assert not other_get.called
    assert not other_put.called


async def test_approved_run_is_refused_when_the_card_has_no_user_id(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _, put = _mount(mock, [])
        lookup = mock.get(f"/admin/realms/{_REALM}/users").respond(500)
        request_id = await _park({"username": "event-guest", "add": ["CONFIGURE_TOTP"]})
        lookup.respond(200, json=[{"id": _USER_UUID, "username": "event-guest"}])
        resumed = await _approve_and_resume(request_id)
    assert resumed.status != "ok"
    assert "approval card shows no user id" in str(resumed.model_dump(mode="json"))
    assert not put.called


async def test_card_still_shows_the_request_when_the_user_cannot_be_read(
    kc: KeycloakConnector,
) -> None:
    requester = _operator("kc-otp-requester")
    args = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": {"username": "event-guest", "remove": ["CONFIGURE_TOTP"]},
    }
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount(mock, [])
        mock.get(f"/admin/realms/{_REALM}/users").respond(500)
        call = await call_operation(requester, args)
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        async with get_sessionmaker()() as session:
            pending = await session.get(ApprovalRequest, request_id)
            assert pending is not None
            preview = pending.proposed_effect["preview"]
    assert preview["username"] == "event-guest"
    assert preview["remove"] == ["CONFIGURE_TOTP"]
    assert preview["current_available"] is False


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------

_SECRET_DATA = '{"value":"made-up-otp-seed-1234","algorithm":"HmacSHA1"}'


def _credentials(*types: str) -> list[dict[str, Any]]:
    return [
        {
            "id": f"cred-{index}",
            "type": kind,
            "userLabel": "made-up label",
            "secretData": _SECRET_DATA,
            "credentialData": '{"digits":6}',
        }
        for index, kind in enumerate(types)
    ]


def _users(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"{index:08d}-0000-0000-0000-000000000000",
            "username": f"user-{index}",
            "enabled": True,
            "requiredActions": ["CONFIGURE_TOTP"] if index == 1 else [],
        }
        for index in range(count)
    ]


def _credential_path(user: dict[str, Any]) -> str:
    return f"/admin/realms/{_REALM}/users/{user['id']}/credentials"


async def test_status_shows_actions_and_otp_and_no_credential_value(
    kc: KeycloakConnector,
) -> None:
    users = _users(3)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        listing = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=users)
        mock.get(_credential_path(users[0])).respond(200, json=_credentials("password", "otp"))
        mock.get(_credential_path(users[1])).respond(200, json=_credentials("password"))
        mock.get(_credential_path(users[2])).respond(200, json=[])
        result = await _run(_OTP_OP_ID, {})
    assert result["status"] == "ok", result
    params = listing.calls.last.request.url.params
    assert params["briefRepresentation"] == "false"
    assert params["max"] == "101"
    data = result["result"]
    assert [row["has_otp"] for row in data["rows"]] == [True, False, False]
    assert [row["otp_setup_pending"] for row in data["rows"]] == [False, True, False]
    assert data["rows"][1]["required_actions"] == ["CONFIGURE_TOTP"]
    assert data["total"] == 3
    assert data["unknown"] == 0
    assert data["truncated"] is False
    dumped = json.dumps(result)
    for secret in ("made-up-otp-seed-1234", "made-up label", "HmacSHA1", "secretData"):
        assert secret not in dumped


async def test_status_unreadable_credentials_are_unknown_not_false(
    kc: KeycloakConnector,
) -> None:
    users = _users(2)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=users)
        mock.get(_credential_path(users[0])).respond(403, json={"error": "forbidden"})
        mock.get(_credential_path(users[1])).respond(200, json={"error": "odd body"})
        result = await _run(_OTP_OP_ID, {})
    assert result["status"] == "ok", result
    assert [row["has_otp"] for row in result["result"]["rows"]] == [None, None]
    assert result["result"]["unknown"] == 2


async def test_status_says_when_the_list_was_cut(kc: KeycloakConnector) -> None:
    users = _users(3)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        listing = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=users)
        for user in users:
            mock.get(_credential_path(user)).respond(200, json=[])
        result = await _run(_OTP_OP_ID, {"max": 2, "username": "user"})
    assert result["status"] == "ok", result
    assert listing.calls.last.request.url.params["max"] == "3"
    assert listing.calls.last.request.url.params["username"] == "user"
    assert result["result"]["total"] == 2
    assert result["result"]["truncated"] is True


async def test_status_empty_realm_and_missing_actions_field(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        listing = mock.get(f"/admin/realms/{_REALM}/users").respond(200, json=[])
        empty = await _run(_OTP_OP_ID, {})
        user = {"id": _USER_UUID, "username": "event-guest"}
        listing.respond(200, json=[user])
        mock.get(f"/admin/realms/{_REALM}/users/{_USER_UUID}/credentials").respond(
            200, json=_credentials("otp")
        )
        brief = await _run(_OTP_OP_ID, {})
    assert empty["result"] == {"rows": [], "total": 0, "unknown": 0, "truncated": False}
    row = brief["result"]["rows"][0]
    assert row["required_actions"] is None
    assert row["otp_setup_pending"] is None
    assert row["has_otp"] is True


async def test_status_user_list_that_is_not_a_list_is_an_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _mount_token(mock)
        mock.get(f"/admin/realms/{_REALM}/users").respond(200, json={"error": "odd body"})
        result = await _run(_OTP_OP_ID, {})
    assert result["status"] != "ok"
    assert "expected a JSON array" in str(result)


async def test_status_user_list_failure_is_an_error(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{_REALM}/users").respond(500, json={"error": "boom"})
        result = await _run(_OTP_OP_ID, {})
    assert result["status"] != "ok"
