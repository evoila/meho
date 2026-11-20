# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""More writes that must not lock MEHO out of a Keycloak.

``test_connectors_keycloak_user_login_guard.py`` covers disable and the
required actions write. This file covers the other writes that can take the
sign-in or the admin rights away from the user the target signs in with,
through the real dispatch stack against a respx-mocked Keycloak Admin REST
API (made-up names and ids):

* ``keycloak.user.reset_password`` (``temporary`` true and false, by id and
  by user name), ``keycloak.user.credential.delete``,
  ``keycloak.group.member.remove``, ``keycloak.role_mapping.client.remove``
  and ``keycloak.user.update`` of the user name or the email refuse that
  user before approval and when the approved request runs, and write
  nothing;
* ``keycloak.user.update`` of a first name, and ``keycloak.user.logout``,
  still wait for approval: they cannot lock MEHO out;
* the secret move sink for a Keycloak password refuses that user before
  approval and when the move runs, and lets another user through;
* a target that logs in with the user's email (not its user name) is
  refused the same way, for every guarded write;
* a plain import of the package registers the checks before approval.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest
import respx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_user_login_guard import LOGIN_USER_WRITES
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakPasswordCredentials,
)
from tests.test_connectors_keycloak_user_login_guard import (  # noqa: F401  (fixtures)
    _ADMIN_REALM,
    _KC_BASE_URL,
    _LOGIN_USER,
    _LOGIN_USER_UUID,
    _SAME_REALM_TARGET,
    _call,
    _Keycloak,
    _required_settings_env,
    _reset_module_state,
    _run,
    _stored_requests,
    _user,
    _wire,
    kc,
)

_GROUP_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_PASSWORD_REF = {"password_secret_ref": "kv/made-up/password"}

#: Each guarded write, the words of its refusal, and its other params.
_GUARDED: list[tuple[str, str, dict[str, Any]]] = [
    ("keycloak.user.reset_password", "resetting its password", {**_PASSWORD_REF}),
    (
        "keycloak.user.reset_password",
        "resetting its password",
        {**_PASSWORD_REF, "temporary": True},
    ),
    (
        "keycloak.user.reset_password",
        "resetting its password",
        {**_PASSWORD_REF, "temporary": False},
    ),
    (
        "keycloak.user.credential.delete",
        "deleting one of its credentials",
        {"credential_type": "password"},
    ),
    ("keycloak.group.member.remove", "removing it from a group", {"group_id": _GROUP_UUID}),
    (
        "keycloak.role_mapping.client.remove",
        "removing one of its roles",
        {"client_id": "realm-management", "roles": ["manage-users"]},
    ),
    (
        "keycloak.user.update",
        "changing its user name or email",
        {"changes": {"username": "renamed-admin"}},
    ),
    (
        "keycloak.user.update",
        "changing its user name or email",
        {"changes": {"email": "other@example.invalid"}},
    ),
]
_IDS = [
    "reset_password",
    "reset_password-temporary",
    "reset_password-not-temporary",
    "credential.delete",
    "group.member.remove",
    "role_mapping.client.remove",
    "update-username",
    "update-email",
]


def _named(op_id: str, by: str) -> dict[str, Any]:
    """The params that name the sign-in user, by id or by user name."""
    if by == "username":
        return {"username": _LOGIN_USER}
    return {"user_id" if op_id == "keycloak.group.member.remove" else "id": _LOGIN_USER_UUID}


def _keycloak(mock: respx.MockRouter) -> _Keycloak:
    api = _Keycloak(mock, _ADMIN_REALM)
    api.add(_user(_LOGIN_USER, [], user_id=_LOGIN_USER_UUID))
    mock.get(f"/admin/realms/{_ADMIN_REALM}/groups/{_GROUP_UUID}").respond(
        200, json={"id": _GROUP_UUID, "name": "admins", "path": "/admins"}
    )
    return api


def _writes(mock: respx.MockRouter) -> list[str]:
    return [
        f"{call.request.method} {call.request.url.path}"
        for call in mock.calls
        if call.request.method in {"PUT", "POST", "DELETE"} and "/admin/" in call.request.url.path
    ]


@pytest.mark.parametrize("by", ["id", "username"])
@pytest.mark.parametrize(("op_id", "action", "params"), _GUARDED, ids=_IDS)
async def test_the_sign_in_user_is_refused_when_it_runs(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    action: str,
    params: dict[str, Any],
    by: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _keycloak(mock)
        result = await _run(op_id, {**_named(op_id, by), **params}, _SAME_REALM_TARGET)
        written = _writes(mock)
    assert result["status"] != "ok", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteRefusedError", result
    message = result["extras"]["exception_message"]
    # reset_password sends a secret, so MEHO withholds its error text.
    if "withheld" not in message:
        assert "target_login_user" in message
        assert action in message
    assert written == []


@pytest.mark.parametrize("by", ["id", "username"])
@pytest.mark.parametrize(("op_id", "action", "params"), _GUARDED, ids=_IDS)
async def test_the_sign_in_user_is_refused_before_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    action: str,
    params: dict[str, Any],
    by: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _keycloak(mock)
        call = await _call(op_id, {**_named(op_id, by), **params}, _SAME_REALM_TARGET)
        written = _writes(mock)
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert action in call["error"]
    assert _LOGIN_USER not in call["error"]
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("keycloak.user.update", {"changes": {"firstName": "Eve"}}),
        ("keycloak.user.logout", {}),
    ],
    ids=["update-first-name", "logout"],
)
async def test_a_write_that_cannot_lock_meho_out_still_waits(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _keycloak(mock)
        call = await _call(op_id, {"id": _LOGIN_USER_UUID, **params}, _SAME_REALM_TARGET)
    assert call["status"] == "awaiting_approval", call


async def test_another_user_is_not_refused(kc: KeycloakConnector) -> None:  # noqa: F811
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        api.add(_user("event-guest", []))
        call = await _call(
            "keycloak.user.reset_password",
            {"username": "event-guest", **_PASSWORD_REF},
            _SAME_REALM_TARGET,
        )
    assert call["status"] == "awaiting_approval", call


# ---------------------------------------------------------------------------
# The target logs in with the user's email
# ---------------------------------------------------------------------------

_LOGIN_EMAIL = "Break-Glass@Example.invalid"

#: Every guarded write by id, with the params it needs.
_EMAIL_LOGIN_WRITES: list[tuple[str, dict[str, Any]]] = [
    ("keycloak.user.disable", {}),
    ("keycloak.user.required_actions.update", {"add": ["UPDATE_PASSWORD"]}),
    *[(op_id, params) for op_id, _action, params in _GUARDED],
]
_EMAIL_IDS = ["disable", "required_actions.update", *_IDS]


def _email_loader(_target: Any, _operator: Operator) -> Any:
    async def _load() -> KeycloakAdminCredentials:
        return KeycloakPasswordCredentials(username=_LOGIN_EMAIL, password="stub-password")

    return _load()


@pytest.fixture
async def kc_email(kc: KeycloakConnector) -> AsyncIterator[KeycloakConnector]:  # noqa: F811
    """The same targets, but the target logs in with the email of the user."""
    connector = _wire(_email_loader)
    yield connector
    await connector.aclose()


def _email_keycloak(mock: respx.MockRouter) -> _Keycloak:
    api = _keycloak(mock)
    api.users[_LOGIN_USER_UUID]["email"] = _LOGIN_EMAIL.lower()
    return api


@pytest.mark.parametrize(("op_id", "params"), _EMAIL_LOGIN_WRITES, ids=_EMAIL_IDS)
async def test_email_sign_in_user_is_refused_before_approval(
    kc_email: KeycloakConnector,
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _email_keycloak(mock)
        call = await _call(op_id, {**_named(op_id, "id"), **params}, _SAME_REALM_TARGET)
        written = _writes(mock)
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval"
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize(("op_id", "params"), _EMAIL_LOGIN_WRITES, ids=_EMAIL_IDS)
async def test_email_sign_in_user_is_refused_when_it_runs(
    kc_email: KeycloakConnector,
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _email_keycloak(mock)
        result = await _run(op_id, {**_named(op_id, "id"), **params}, _SAME_REALM_TARGET)
        written = _writes(mock)
    assert result["status"] != "ok", result
    assert result["extras"]["exception_class"] == "KeycloakDeleteRefusedError", result
    assert written == []


async def test_email_sign_in_does_not_refuse_a_user_with_another_email(
    kc_email: KeycloakConnector,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _keycloak(mock)
        api.users[_LOGIN_USER_UUID]["email"] = "someone-else@example.invalid"
        call = await _call("keycloak.user.disable", {"id": _LOGIN_USER_UUID}, _SAME_REALM_TARGET)
    assert call["status"] == "awaiting_approval", call


def test_a_plain_import_registers_the_checks_before_approval() -> None:
    """A new interpreter: importing the packages alone registers every check."""
    ops = [*LOGIN_USER_WRITES, "secret.move"]
    code = (
        "import json\n"
        "import meho_backplane.connectors.keycloak\n"
        "import meho_backplane.connectors.secret\n"
        "from meho_backplane.operations._park_check import _PARK_CHECKS\n"
        f"ops = {ops!r}\n"
        "print(json.dumps({op: getattr(_PARK_CHECKS.get(op), '__qualname__', None) "
        "for op in ops}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    checks = json.loads(done.stdout.strip().splitlines()[-1])
    for op_id in LOGIN_USER_WRITES:
        assert checks[op_id] == "refuse_other_user_check.<locals>._check", (op_id, checks)
    assert checks["secret.move"] == "refuse_sink_before_approval"
