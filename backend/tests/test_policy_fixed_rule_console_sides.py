# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The two sides of the sentences of ``rights_change_waits`` and ``delete_waits``.

``FIXED_RULES`` says that a change of rights and a delete wait, except an
Admin's own change in the web console after a fresh sign-in (a tenant Admin
outside MEHO's own login realm, or a platform admin) that is not destructive
and writes no credential. These tests send ``keycloak.group.member.add`` and
``keycloak.user.credential.delete`` (risk level ``dangerous``) through
``dispatch`` against a respx-mocked Keycloak, and check both sides: who runs
the call at once, and who waits. Fixture values are made up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import respx

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch
from meho_backplane.policy.fixed_rules import DELETE_WAITS, FIXED_RULES, RIGHTS_CHANGE_WAITS
from meho_backplane.targets.resolver import resolve_target
from tests._keycloak_access_guard_helpers import platform_admins_not_counted
from tests.test_rights_change_console_run import (
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _LOGIN_REALMS,
    _OLD_SIGN_IN_AGE,
    _PLAIN_GROUP,
    _REALM,
    _TARGET_NAME,
    _TENANT_ID,
    _USER_UUID,
    _add,
    _mount,
    _person,
    _requests,
    _required_settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
    keycloak,  # noqa: F401 -- fixture: registers the Keycloak operations
)

#: The credential delete counts the platform admins; these tests are about the gate.
pytestmark = pytest.mark.usefixtures(platform_admins_not_counted.__name__)

_DELETE = "keycloak.user.credential.delete"
_DELETE_PARAMS: dict[str, Any] = {"id": _USER_UUID, "credential_type": "otp"}
_USER_PATH = f"/admin/realms/{_REALM}/users/{_USER_UUID}"
_CREDENTIALS_PATH = f"{_USER_PATH}/credentials"
_OTP = {"id": "cred-otp-1", "type": "otp", "userLabel": "phone"}
_PASSWORD = {"id": "cred-pw", "type": "password"}


@dataclass(frozen=True)
class _Case:
    name: str
    #: The arguments of ``_person``. The operator is built when the test runs, so
    #: its sign-in stays fresh however late the test runs.
    person: dict[str, Any]
    login_realm: bool = False
    hosted_run: bool = False
    at_once: bool = False

    def operator(self) -> Operator:
        return _person(**self.person)


_TENANT_ADMIN: dict[str, Any] = {}
_PLATFORM_ADMIN: dict[str, Any] = {"platform_admin": True}
_CASES = [
    _Case("console-tenant-admin-outside-the-login-realm", _TENANT_ADMIN, at_once=True),
    _Case("console-platform-admin", _PLATFORM_ADMIN, at_once=True),
    _Case(
        "console-platform-admin-in-the-login-realm",
        _PLATFORM_ADMIN,
        login_realm=True,
        at_once=True,
    ),
    _Case("console-tenant-admin-in-the-login-realm", _TENANT_ADMIN, login_realm=True),
    _Case("console-admin-old-sign-in", {"sign_in_age": _OLD_SIGN_IN_AGE}),
    _Case(
        "console-platform-admin-old-sign-in",
        {"platform_admin": True, "sign_in_age": _OLD_SIGN_IN_AGE},
    ),
    _Case("console-operator", {"role": TenantRole.OPERATOR}),
    _Case("tenant-admin-with-a-token", {"web": False}),
    _Case("platform-admin-with-a-token", {"platform_admin": True, "web": False}),
    _Case("hosted-agent-run-of-an-admin", _TENANT_ADMIN, hosted_run=True),
]
_IDS = [case.name for case in _CASES]


def _mount_delete(mock: respx.MockRouter) -> respx.Route:
    """Keycloak answers for the credential delete. Returns the DELETE."""
    mock.post("/realms/master/protocol/openid-connect/token").respond(
        200, json={"access_token": "kc-admin-token-console", "expires_in": 300}
    )
    mock.get(f"/admin/realms/{_REALM}").respond(200, json={"realm": _REALM})
    mock.get(_USER_PATH).respond(200, json={"id": _USER_UUID, "username": "operator-a"})
    mock.get(f"{_USER_PATH}/groups").respond(200, json=[])
    mock.get(_CREDENTIALS_PATH).respond(200, json=[_OTP, _PASSWORD])
    return mock.delete(f"{_CREDENTIALS_PATH}/{_OTP['id']}").respond(204)


async def _delete(operator: Operator) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=_DELETE,
        target=target,
        params=_DELETE_PARAMS,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _outside_the_login_realm(monkeypatch: pytest.MonkeyPatch, case: _Case) -> None:
    if not case.login_realm:
        monkeypatch.setattr(_LOGIN_REALMS, lambda: {"another-login-realm"})


async def _assert_side(case: _Case, result: dict[str, Any], write: respx.Route) -> None:
    if case.at_once:
        assert result["status"] == "ok", result
        assert write.called
        assert await _requests() == []
        return
    assert result["status"] == "awaiting_approval", result
    assert not write.called
    assert len(await _requests()) == 1


def test_the_sentences_name_who_runs_at_once() -> None:
    assert FIXED_RULES[RIGHTS_CHANGE_WAITS] == (
        "A change of rights waits for a person, except an Admin's own change in the web "
        "console after a fresh sign-in (a tenant Admin outside MEHO's own login realm, or a "
        "platform admin) that is not destructive and writes no credential."
    )
    assert FIXED_RULES[DELETE_WAITS] == (
        "A delete waits for approval, except an Admin's own change of rights in the web "
        "console that is not destructive, after a fresh sign-in (a tenant Admin outside "
        "MEHO's own login realm, or a platform admin); in sandbox mode a delete grant may "
        "cover a delete."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_a_member_add_runs_at_once_only_for_the_named_admins(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    case: _Case,
) -> None:
    _outside_the_login_realm(monkeypatch, case)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        put = _mount(mock, group=_PLAIN_GROUP)
        if case.hosted_run:
            with actor_delegation("agent:helper"):
                result = await _add(case.operator())
        else:
            result = await _add(case.operator())
    await _assert_side(case, result, put)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_a_credential_delete_runs_at_once_only_for_the_named_admins(
    keycloak: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    case: _Case,
) -> None:
    _outside_the_login_realm(monkeypatch, case)
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        delete = _mount_delete(mock)
        if case.hosted_run:
            with actor_delegation("agent:helper"):
                result = await _delete(case.operator())
        else:
            result = await _delete(case.operator())
    await _assert_side(case, result, delete)
