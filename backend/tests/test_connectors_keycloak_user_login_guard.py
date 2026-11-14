# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""User writes that could lock MEHO out of a Keycloak, and the card lines for sign-in steps.

Drives ``keycloak.user.required_actions.update`` and ``keycloak.user.disable``
through the real dispatch stack against a respx-mocked Keycloak Admin REST API
(made-up names and ids). No running Keycloak and no Vault.

What is proven:

* both writes refuse the user the target signs in with (a user name and
  password, or a client's service-account user), before approval and when
  the approved request runs, and write nothing;
* a request that waits for approval, then finds the user renamed to the
  sign-in user, is refused when it runs;
* another user in the same realm is not refused;
* an unknown action name in ``add`` is refused before approval;
* in MEHO's own sign-in realm, the card says when a change adds or removes a
  sign-in set-up step, and has no such line in another realm or for a
  change that touches no sign-in step;
* the result's ``required_actions`` is the list read back from Keycloak, and
  is ``null`` with ``read_back_checked: false`` when the read-back fails.
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
from meho_backplane.connectors.keycloak import (  # noqa: F401 -- import registers the park checks
    KeycloakConnector,
    ops_user_login_guard,
    ops_user_required_actions,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakAdminCredentialsLoader,
    KeycloakClientCredentials,
    KeycloakPasswordCredentials,
    KeycloakTargetLike,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
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

_UPDATE_OP = "keycloak.user.required_actions.update"
_DISABLE_OP = "keycloak.user.disable"
_CONNECTOR_ID = "keycloak-admin-26.x"
# The managed realm is the realm the target signs in to, so the guard applies.
_SAME_REALM_TARGET = "kc-guard-same-realm"
# The managed realm is MEHO's own sign-in realm; the target signs in to another one.
_LOGIN_REALM_TARGET = "kc-guard-login-realm"
_OTHER_REALM_TARGET = "kc-guard-other-realm"
_KC_HOST = "keycloak-guard-e2e.test.invalid"
_KC_BASE_URL = f"https://{_KC_HOST}"
_ADMIN_REALM = "master"
_LOGIN_REALM = "meho"
_OTHER_REALM = "partners"
_LOGIN_USER = "break-glass-admin"
_USER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_LOGIN_USER_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000006381")


def _operator(sub: str = "kc-guard-requester", role: TenantRole = TenantRole.OPERATOR) -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Guard Test Operator",
        email=None,
        raw_jwt="<kc-guard-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=PrincipalKind.USER,
    )


def _password_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakPasswordCredentials(username=_LOGIN_USER, password="stub-password")

    return _load()


def _client_loader(_target: KeycloakTargetLike, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")

    return _load()


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", f"https://keycloak.test/realms/{_LOGIN_REALM}")
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


def _wire(loader: KeycloakAdminCredentialsLoader) -> KeycloakConnector:
    connector = KeycloakConnector(credentials_loader=loader)
    _CONNECTOR_INSTANCE_CACHE[KeycloakConnector] = connector
    return connector


@pytest.fixture
async def kc() -> AsyncIterator[KeycloakConnector]:
    set_default_reducer(PassThroughReducer())
    await KeycloakConnector.register_operations()
    async with get_sessionmaker()() as session:
        for name, realm in (
            (_SAME_REALM_TARGET, _ADMIN_REALM),
            (_LOGIN_REALM_TARGET, _LOGIN_REALM),
            (_OTHER_REALM_TARGET, _OTHER_REALM),
        ):
            session.add(
                TargetORM(
                    tenant_id=_TENANT_ID,
                    name=name,
                    aliases=[],
                    product="keycloak",
                    host=_KC_HOST,
                    port=443,
                    fqdn=None,
                    secret_ref="keycloak/admin",
                    auth_model="shared_service_account",
                    vpn_required=False,
                    extras={"managed_realm": realm, "admin_realm": _ADMIN_REALM},
                    fingerprint={"version": "26.3.3"},
                    notes="seeded by test_connectors_keycloak_user_login_guard",
                )
            )
        await session.commit()
    connector = _wire(_password_loader)
    yield connector
    await connector.aclose()


_ALLOWED = [
    {"alias": "CONFIGURE_TOTP", "enabled": True},
    {"alias": "CONFIGURE_RECOVERY_AUTHN_CODES", "enabled": True},
    {"alias": "webauthn-register", "enabled": True},
    {"alias": "VERIFY_EMAIL", "enabled": True},
    {"alias": "UPDATE_PASSWORD", "enabled": True},
]


def _user(
    username: str, actions: list[str], *, user_id: str = _USER_UUID, enabled: bool = True
) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": username,
        "enabled": enabled,
        "firstName": "Eve",
        "requiredActions": actions,
    }


class _Keycloak:
    """The mocked Admin API of one realm: users by id, the PUT calls, the read-back."""

    def __init__(self, mock: respx.MockRouter, realm: str) -> None:
        self.realm = realm
        self.users: dict[str, dict[str, Any]] = {}
        self.put_ids: list[str] = []
        self.after: dict[str, dict[str, Any]] = {}
        self.read_back_fails = False
        self.read_back_extra: list[str] = []
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"/admin/realms/{realm}/authentication/required-actions").respond(
            200, json=_ALLOWED
        )
        mock.get(f"/admin/realms/{realm}/users").mock(side_effect=self._find)
        mock.route(url__regex=rf".*/admin/realms/{realm}/users/[^/]+$").mock(
            side_effect=self._user_route
        )

    def add(self, user: dict[str, Any]) -> None:
        self.users[user["id"]] = user

    def _find(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("username", "")
        rows = [u for u in self.users.values() if u["username"].casefold() == name.casefold()]
        return httpx.Response(200, json=[{"id": u["id"], "username": u["username"]} for u in rows])

    def _user_route(self, request: httpx.Request) -> httpx.Response:
        user_id = request.url.path.rsplit("/", 1)[1]
        user = self.users.get(user_id)
        if request.method == "PUT":
            self.put_ids.append(user_id)
            body = json.loads(request.content)
            self.after[user_id] = body
            return httpx.Response(204)
        if user is None:
            return httpx.Response(404)
        if user_id in self.after:
            if self.read_back_fails:
                return httpx.Response(500)
            body = self.after[user_id]
            held = [*body["requiredActions"], *self.read_back_extra]
            return httpx.Response(200, json={**body, "requiredActions": held})
        return httpx.Response(200, json=user)


async def _target(name: str) -> Any:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT_ID, name)


async def _run(op_id: str, params: dict[str, Any], target: str) -> dict[str, Any]:
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=await _target(target),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


async def _call(op_id: str, params: dict[str, Any], target: str) -> dict[str, Any]:
    return await call_operation(
        _operator(),
        {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": target, "params": params},
    )


async def _stored_requests() -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _card_of(request: Any) -> dict[str, Any]:
    card: dict[str, Any] = request.proposed_effect["preview"]
    return card


# ---------------------------------------------------------------------------
# The sign-in user is refused
# ---------------------------------------------------------------------------

_UPDATE_LOGIN_PARAMS = {"username": _LOGIN_USER, "add": ["UPDATE_PASSWORD"]}


async def test_required_actions_update_refuses_the_sign_in_user_when_it_runs(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user(_LOGIN_USER, [], user_id=_LOGIN_USER_UUID))
        result = await _run(_UPDATE_OP, _UPDATE_LOGIN_PARAMS, _SAME_REALM_TARGET)
    assert result["status"] != "ok"
    assert "target_login_user" in str(result)
    assert "changing its required actions" in str(result)
    assert api.put_ids == []


async def test_required_actions_remove_is_refused_for_the_sign_in_user_too(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user(_LOGIN_USER, ["UPDATE_PASSWORD"], user_id=_LOGIN_USER_UUID))
        result = await _run(
            _UPDATE_OP, {"id": _LOGIN_USER_UUID, "remove": ["UPDATE_PASSWORD"]}, _SAME_REALM_TARGET
        )
    assert result["status"] != "ok"
    assert "target_login_user" in str(result)
    assert api.put_ids == []


async def test_required_actions_update_is_refused_before_approval(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user(_LOGIN_USER, [], user_id=_LOGIN_USER_UUID))
        call = await _call(_UPDATE_OP, _UPDATE_LOGIN_PARAMS, _SAME_REALM_TARGET)
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "logs in with" in call["error"]
    assert _LOGIN_USER not in call["error"]
    assert await _stored_requests() == []
    assert api.put_ids == []


async def test_disable_refuses_the_sign_in_user_when_it_runs(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user(_LOGIN_USER, [], user_id=_LOGIN_USER_UUID))
        by_name = await _run(_DISABLE_OP, {"username": _LOGIN_USER}, _SAME_REALM_TARGET)
        by_id = await _run(_DISABLE_OP, {"id": _LOGIN_USER_UUID}, _SAME_REALM_TARGET)
    for result in (by_name, by_id):
        assert result["status"] != "ok"
        assert "target_login_user" in str(result)
        assert "disabling it" in str(result)
    assert api.put_ids == []


async def test_disable_is_refused_before_approval(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user(_LOGIN_USER, [], user_id=_LOGIN_USER_UUID))
        call = await _call(_DISABLE_OP, {"id": _LOGIN_USER_UUID}, _SAME_REALM_TARGET)
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "logs in with" in call["error"]
    assert await _stored_requests() == []
    assert api.put_ids == []


async def test_the_service_account_user_of_a_client_login_is_refused(
    kc: KeycloakConnector,
) -> None:
    kc = _wire(_client_loader)
    service_user = "service-account-meho-admin"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user(service_user, [], user_id=_LOGIN_USER_UUID))
        update = await _run(
            _UPDATE_OP, {"username": service_user, "add": ["UPDATE_PASSWORD"]}, _SAME_REALM_TARGET
        )
        disable = await _run(_DISABLE_OP, {"username": service_user}, _SAME_REALM_TARGET)
    await kc.aclose()
    assert update["status"] != "ok"
    assert disable["status"] != "ok"
    assert api.put_ids == []


async def test_another_user_in_the_same_realm_is_not_refused(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user("event-guest", ["VERIFY_EMAIL"]))
        update = await _run(
            _UPDATE_OP, {"username": "event-guest", "add": ["UPDATE_PASSWORD"]}, _SAME_REALM_TARGET
        )
        disable = await _run(_DISABLE_OP, {"username": "event-guest"}, _SAME_REALM_TARGET)
        waiting = await _call(
            _UPDATE_OP, {"username": "event-guest", "remove": ["VERIFY_EMAIL"]}, _SAME_REALM_TARGET
        )
    assert update["status"] == "ok", update
    assert disable["status"] == "ok", disable
    assert waiting["status"] == "awaiting_approval", waiting
    assert api.put_ids == [_USER_UUID, _USER_UUID]


async def test_the_sign_in_user_name_of_another_realm_is_not_refused(
    kc: KeycloakConnector,
) -> None:
    """A user with the sign-in name in another realm is a different user."""
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _OTHER_REALM)
        api.add(_user(_LOGIN_USER, []))
        result = await _run(_DISABLE_OP, {"username": _LOGIN_USER}, _OTHER_REALM_TARGET)
    assert result["status"] == "ok", result
    assert api.put_ids == [_USER_UUID]


async def test_a_user_renamed_to_the_sign_in_user_while_the_request_waits_is_refused_on_run(
    kc: KeycloakConnector,
) -> None:
    """The real path: park, rename, a second person approves, MEHO refuses and writes nothing."""
    approver = _operator("kc-guard-approver", TenantRole.TENANT_ADMIN)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _ADMIN_REALM)
        api.add(_user("event-guest", ["VERIFY_EMAIL"]))
        call = await _call(
            _UPDATE_OP, {"id": _USER_UUID, "add": ["UPDATE_PASSWORD"]}, _SAME_REALM_TARGET
        )
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        # While the request waits, the account is renamed to the name the target signs in with.
        api.users[_USER_UUID]["username"] = _LOGIN_USER
        async with get_sessionmaker()() as session:
            await approve_request(session, request_id, operator=approver, params=None)
            await session.commit()
        async with get_sessionmaker()() as session:
            row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            resumed = await resume_dispatch_after_approval(
                operator=approver, request=row, params=None
            )
    assert resumed.status != "ok", resumed
    assert "target_login_user" in str(resumed.model_dump(mode="json"))
    assert api.put_ids == []


# ---------------------------------------------------------------------------
# An unknown action name is refused before approval
# ---------------------------------------------------------------------------


async def test_an_unknown_action_name_is_refused_before_approval(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _OTHER_REALM)
        api.add(_user("event-guest", []))
        call = await _call(
            _UPDATE_OP,
            {"username": "event-guest", "add": ["VERIFY_EMAIL", "MADE_UP_ACTION"]},
            _OTHER_REALM_TARGET,
        )
        removal = await _call(
            _UPDATE_OP,
            {"username": "event-guest", "remove": ["MADE_UP_ACTION"]},
            _OTHER_REALM_TARGET,
        )
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert "not an enabled required action" in call["error"]
    assert "MADE_UP_ACTION" not in call["error"]
    assert "VERIFY_EMAIL" in call["error"]  # the allowed names are listed
    # A removal needs no check: taking off a name that is not there changes nothing.
    assert removal["status"] == "awaiting_approval", removal
    assert api.put_ids == []
    assert len(await _stored_requests()) == 1


# ---------------------------------------------------------------------------
# The card lines
# ---------------------------------------------------------------------------


async def _card(target: str, realm: str, params: dict[str, Any]) -> dict[str, Any]:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, realm)
        api.add(_user("event-guest", ["CONFIGURE_TOTP", "webauthn-register", "VERIFY_EMAIL"]))
        call = await _call(_UPDATE_OP, {"username": "event-guest", **params}, target)
    assert call["status"] == "awaiting_approval", call
    request = await _get_request(uuid.UUID(call["extras"]["approval_request_id"]))
    return _card_of(request)


async def _get_request(request_id: uuid.UUID) -> Any:
    async with get_sessionmaker()() as session:
        request = await session.get(ApprovalRequest, request_id)
    assert request is not None
    return request


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"remove": ["CONFIGURE_TOTP"]}, "takes a sign-in set-up step away (CONFIGURE_TOTP)"),
        ({"remove": ["webauthn-register"]}, "takes a sign-in set-up step away (webauthn-register)"),
        (
            {"remove": ["CONFIGURE_RECOVERY_AUTHN_CODES"]},
            "takes a sign-in set-up step away (CONFIGURE_RECOVERY_AUTHN_CODES)",
        ),
        (
            {"add": ["CONFIGURE_RECOVERY_AUTHN_CODES"]},
            "gives the person a new way to sign in (CONFIGURE_RECOVERY_AUTHN_CODES)",
        ),
        ({"add": ["CONFIGURE_TOTP"]}, "gives the person a new way to sign in (CONFIGURE_TOTP)"),
    ],
)
async def test_card_has_a_line_for_a_sign_in_step_in_the_sign_in_realm(
    kc: KeycloakConnector, params: dict[str, Any], expected: str
) -> None:
    card = await _card(_LOGIN_REALM_TARGET, _LOGIN_REALM, params)
    assert expected in card["warning"]


async def test_card_lines_add_up_when_one_change_does_both(kc: KeycloakConnector) -> None:
    card = await _card(
        _LOGIN_REALM_TARGET,
        _LOGIN_REALM,
        {"add": ["CONFIGURE_RECOVERY_AUTHN_CODES"], "remove": ["CONFIGURE_TOTP"]},
    )
    assert "new way to sign in" in card["warning"]
    assert "takes a sign-in set-up step away" in card["warning"]


async def test_card_has_no_sign_in_line_elsewhere(kc: KeycloakConnector) -> None:
    other_realm = await _card(_OTHER_REALM_TARGET, _OTHER_REALM, {"remove": ["CONFIGURE_TOTP"]})
    other_step = await _card(_LOGIN_REALM_TARGET, _LOGIN_REALM, {"remove": ["VERIFY_EMAIL"]})
    assert "warning" not in other_realm
    assert "warning" not in other_step


# ---------------------------------------------------------------------------
# The result shows what Keycloak holds
# ---------------------------------------------------------------------------


async def test_result_lists_the_required_actions_read_back_from_keycloak(
    kc: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _OTHER_REALM)
        api.add(_user("event-guest", ["VERIFY_EMAIL"]))
        # Keycloak holds one more action than MEHO sent.
        api.read_back_extra = ["UPDATE_PASSWORD"]
        result = await _run(
            _UPDATE_OP, {"username": "event-guest", "add": ["CONFIGURE_TOTP"]}, _OTHER_REALM_TARGET
        )
    assert result["status"] == "ok", result
    assert result["result"]["required_actions"] == [
        "VERIFY_EMAIL",
        "CONFIGURE_TOTP",
        "UPDATE_PASSWORD",
    ]
    assert result["result"]["read_back_checked"] is True


async def test_result_says_unknown_when_the_read_back_fails(kc: KeycloakConnector) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _Keycloak(mock, _OTHER_REALM)
        api.read_back_fails = True
        api.add(_user("event-guest", ["VERIFY_EMAIL"]))
        result = await _run(
            _UPDATE_OP, {"username": "event-guest", "add": ["CONFIGURE_TOTP"]}, _OTHER_REALM_TARGET
        )
    assert result["status"] == "ok", result
    assert api.put_ids == [_USER_UUID]
    assert result["result"]["changed"] is True
    assert result["result"]["added"] == ["CONFIGURE_TOTP"]
    assert result["result"]["required_actions"] is None
    assert result["result"]["read_back_checked"] is False
