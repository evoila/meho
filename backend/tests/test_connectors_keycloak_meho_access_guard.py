# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak user writes in MEHO's login realm cannot take MEHO's own access away.

Drives the user writes through the real dispatch stack against a
respx-mocked Keycloak Admin REST API (made-up names and ids). The target
manages MEHO's own login realm. What is proven, for each guarded write:

* a login MEHO itself uses (the service-account user of
  ``KEYCLOAK_ADMIN_CLIENT_ID``) is refused before approval and when the call
  runs, also when the setting names it only after the request waited;
* the last active platform admin is refused before approval and when the
  call runs, also when the other platform admin left while the request
  waited; with a second platform admin the write waits for approval;
* the account of the person who asks is refused;
* a standing grant on ``keycloak.user.disable`` lets none of the three through;
* when MEHO cannot read Keycloak, or platform admin rights come from a
  realm role, the write on a platform admin is refused;
* the same writes in another realm are not refused.

Nothing is written to Keycloak when MEHO refuses.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_user_meho_access_guard import (
    LOCK_OUT_WRITES,
    SERVICE_LOGIN_WRITES,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ServicePrincipalGrant
from meho_backplane.operations import dispatch
from meho_backplane.operations.approval_queue import (
    ApprovalGroupForbiddenError,
    ApprovalRoleFloorError,
    RightsChangeApprovalError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.test_connectors_keycloak_user_login_guard import (  # noqa: F401  (fixtures)
    _ADMIN_REALM,
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _LOGIN_REALM,
    _LOGIN_REALM_TARGET,
    _OTHER_REALM,
    _OTHER_REALM_TARGET,
    _TENANT_ID,
    _required_settings_env,
    _reset_module_state,
    _stored_requests,
    _target,
    kc,
)

_ADMIN_CLIENT = "meho-backplane-admin"
_SERVICE_LOGIN = f"service-account-{_ADMIN_CLIENT}"
_SERVICE_UUID = "11111111-1111-1111-1111-111111111111"
_ADMIN = "only-platform-admin"
_ADMIN_UUID = "22222222-2222-2222-2222-222222222222"
_SECOND_ADMIN_UUID = "33333333-3333-3333-3333-333333333333"
_REQUESTER_UUID = "44444444-4444-4444-4444-444444444444"
_PLATFORM_GROUP = "55555555-5555-5555-5555-555555555555"
_TEAM_GROUP = "66666666-6666-6666-6666-666666666666"
_PASSWORD_REF = {"password_secret_ref": "kv/made-up/password"}

#: Each lock-out write and its params other than the user.
_LOCK_OUT: list[tuple[str, dict[str, Any]]] = [
    ("keycloak.user.disable", {}),
    ("keycloak.user.delete", {}),
    ("keycloak.user.required_actions.update", {"add": ["UPDATE_PASSWORD"]}),
    ("keycloak.user.reset_password", {**_PASSWORD_REF}),
    ("keycloak.user.credential.delete", {"credential_type": "password"}),
]
_LOCK_OUT_IDS = [op.removeprefix("keycloak.") for op, _ in _LOCK_OUT]

#: The writes refused on a login MEHO itself uses.
_SERVICE: list[tuple[str, dict[str, Any]]] = [
    *_LOCK_OUT,
    ("keycloak.group.member.remove", {"group_id": _TEAM_GROUP}),
    (
        "keycloak.role_mapping.client.remove",
        {"client_id": "realm-management", "roles": ["manage-users"]},
    ),
    ("keycloak.user.update", {"changes": {"username": "renamed-login"}}),
]
_SERVICE_IDS = [op.removeprefix("keycloak.") for op, _ in _SERVICE]

#: The last platform admin: the lock-out writes, and leaving the platform admin group.
_LAST_ADMIN: list[tuple[str, dict[str, Any]]] = [
    *_LOCK_OUT,
    ("keycloak.group.member.remove", {"group_id": _PLATFORM_GROUP}),
]
_LAST_ADMIN_IDS = [op.removeprefix("keycloak.") for op, _ in _LAST_ADMIN]


@pytest.fixture(autouse=True)
def _admin_client_env(
    _required_settings_env: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
    monkeypatch.delenv("JWT_PLATFORM_ADMIN_ROLE_NAME", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(
    sub: str = _REQUESTER_UUID,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    kind: PrincipalKind = PrincipalKind.USER,
) -> Operator:
    return Operator(
        sub=sub,
        name="Keycloak Access Test Operator",
        email=None,
        raw_jwt="<kc-access-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=role,
        principal_kind=kind,
    )


def _user(user_id: str, username: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "username": username,
        "enabled": True,
        "firstName": "Eve",
        "requiredActions": [],
        "email": f"{username}@example.invalid",
    }


class _Realm:
    """The mocked Admin API of one realm: users, groups and members."""

    def __init__(self, mock: respx.MockRouter, realm: str) -> None:
        self.realm = realm
        self.users: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {
            _PLATFORM_GROUP: {
                "id": _PLATFORM_GROUP,
                "name": "MEHO-Platform-Admins",
                "path": "/MEHO-Platform-Admins",
                "subGroupCount": 0,
                "attributes": {"platform_admin": ["true"]},
            },
            _TEAM_GROUP: {
                "id": _TEAM_GROUP,
                "name": "team",
                "path": "/team",
                "subGroupCount": 0,
                "attributes": {},
            },
        }
        self.members: dict[str, list[str]] = {_PLATFORM_GROUP: [], _TEAM_GROUP: []}
        self.groups_fail = False
        self.slow = False
        self.members_fail = False
        #: Rows sent after the real ones, to send rows in an odd shape.
        self.extra_members: dict[str, list[Any]] = {}
        self.extra_user_groups: list[Any] = []
        self.roles: dict[str, list[dict[str, Any]]] = {}
        base = f"/admin/realms/{realm}"
        mock.post(f"/realms/{_ADMIN_REALM}/protocol/openid-connect/token").respond(
            200, json={"access_token": "kc-admin-token", "expires_in": 300}
        )
        mock.get(f"{base}/authentication/required-actions").respond(
            200, json=[{"alias": "UPDATE_PASSWORD", "enabled": True}]
        )
        mock.get(f"{base}/users").mock(side_effect=self._find)
        mock.get(f"{base}/groups").mock(side_effect=self._top_groups)
        mock.route(url__regex=rf".*{base}/groups/[^/]+(\?.*)?$").mock(side_effect=self._group)
        mock.get(url__regex=rf".*{base}/groups/[^/]+/children(\?.*)?$").mock(
            side_effect=self._children
        )
        mock.get(url__regex=rf".*{base}/groups/[^/]+/members(\?.*)?$").mock(
            side_effect=self._members
        )
        mock.get(url__regex=rf".*{base}/users/[^/]+/groups(\?.*)?$").mock(
            side_effect=self._user_groups
        )
        mock.route(url__regex=rf".*{base}/users/[^/]+(\?.*)?$").mock(side_effect=self._user)
        mock.route(url__regex=rf".*{base}/users/[^/]+/.+").mock(side_effect=self._user_part)

    def add(self, user: dict[str, Any], *groups: str) -> None:
        self.users[user["id"]] = user
        for group in groups:
            self.members[group].append(user["id"])

    def _fail(self) -> httpx.Response | None:
        return httpx.Response(500) if self.groups_fail else None

    def _find(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("username", "")
        rows = [u for u in self.users.values() if u["username"].casefold() == name.casefold()]
        return httpx.Response(200, json=[{"id": u["id"], "username": u["username"]} for u in rows])

    def _user(self, request: httpx.Request) -> httpx.Response:
        user = self.users.get(request.url.path.rsplit("/", 1)[1])
        if request.method == "PUT" and user is not None:
            body = json.loads(request.content or b"{}")
            if isinstance(body.get("enabled"), bool):
                user["enabled"] = body["enabled"]
        if request.method != "GET":
            return httpx.Response(204)
        return httpx.Response(200, json=user) if user is not None else httpx.Response(404)

    def _user_part(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(204)
        if request.url.path.endswith("/role-mappings/realm/composite"):
            user_id = request.url.path.split("/")[-4]
            return httpx.Response(200, json=self.roles.get(user_id, []))
        if request.url.path.endswith("/credentials"):
            return httpx.Response(200, json=[{"id": "made-up-credential", "type": "password"}])
        return httpx.Response(200, json=[])

    def _top_groups(self, request: httpx.Request) -> httpx.Response:
        rows = [g for g in self.groups.values() if not g.get("parentId")]
        return self._fail() or _page(request, rows)

    def _children(self, request: httpx.Request) -> httpx.Response:
        parent = request.url.path.split("/")[-2]
        rows = [g for g in self.groups.values() if g.get("parentId") == parent]
        return self._fail() or _page(request, rows)

    def group(self, group_id: str, name: str, value: str | None, parent: str | None = None) -> None:
        """Add a group; *value* is its own ``platform_admin`` value, or ``None`` for none."""
        path = f"{self.groups[parent]['path']}/{name}" if parent else f"/{name}"
        self.groups[group_id] = {
            "id": group_id,
            "name": name,
            "path": path,
            "subGroupCount": 0,
            "attributes": {} if value is None else {"platform_admin": [value]},
        }
        if parent:
            self.groups[group_id]["parentId"] = parent
            self.groups[parent]["subGroupCount"] += 1
        self.members[group_id] = []

    def _group(self, request: httpx.Request) -> httpx.Response:
        group = self.groups.get(request.url.path.rsplit("/", 1)[1])
        failed = self._fail()
        if failed is not None:
            return failed
        return httpx.Response(200, json=group) if group is not None else httpx.Response(404)

    async def _members(self, request: httpx.Request) -> httpx.Response:
        if self.slow:
            # Lets another call run between the count and the write.
            await asyncio.sleep(0.05)
        group_id = request.url.path.split("/")[-2]
        rows: list[Any] = [self.users[uid] for uid in self.members.get(group_id, [])]
        rows += self.extra_members.get(group_id, [])
        if self.members_fail:
            return httpx.Response(500)
        return self._fail() or _page(request, rows)

    def _user_groups(self, request: httpx.Request) -> httpx.Response:
        user_id = request.url.path.split("/")[-2]
        rows: list[Any] = [self.groups[g] for g, ids in self.members.items() if user_id in ids]
        rows += self.extra_user_groups
        return self._fail() or _page(request, rows)


def _page(request: httpx.Request, rows: list[Any]) -> httpx.Response:
    """One page of *rows*, as Keycloak sends it for ``first`` and ``max``."""
    first = int(request.url.params.get("first", "0"))
    size = int(request.url.params.get("max", str(len(rows) or 1)))
    return httpx.Response(200, json=rows[first : first + size])


def _realm(mock: respx.MockRouter, realm: str = _LOGIN_REALM) -> _Realm:
    api = _Realm(mock, realm)
    api.add(_user(_SERVICE_UUID, _SERVICE_LOGIN), _TEAM_GROUP)
    api.add(_user(_ADMIN_UUID, _ADMIN), _PLATFORM_GROUP)
    api.add(_user(_REQUESTER_UUID, "the-requester"), _TEAM_GROUP)
    return api


def _writes(mock: respx.MockRouter) -> list[str]:
    return [
        f"{call.request.method} {call.request.url.path}"
        for call in mock.calls
        if call.request.method in {"PUT", "POST", "DELETE"} and "/admin/" in call.request.url.path
    ]


def _params(op_id: str, user_id: str, extra: dict[str, Any]) -> dict[str, Any]:
    key = "user_id" if op_id.startswith("keycloak.group.member.") else "id"
    return {key: user_id, **extra}


async def _call(
    op_id: str, params: dict[str, Any], operator: Operator | None = None
) -> dict[str, Any]:
    """Call the op as an agent does; a delete first asks for its preview, as MEHO requires."""
    caller = operator or _operator()
    args: dict[str, Any] = {
        "connector_id": _CONNECTOR_ID,
        "op_id": op_id,
        "target": _LOGIN_REALM_TARGET,
        "params": params,
    }
    if op_id in {"keycloak.user.delete", "keycloak.group.delete"}:
        preview = await preview_operation(caller, args)
        if preview.get("preview_hash"):
            args["preview_hash"] = preview["preview_hash"]
    return await call_operation(caller, args)


async def _run(
    op_id: str, params: dict[str, Any], target: str = _LOGIN_REALM_TARGET
) -> dict[str, Any]:
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


def _refused_on_run(result: dict[str, Any], code: str) -> None:
    assert result["status"] != "ok", result
    assert result["extras"]["exception_class"] == "MehoAccessRefusedError", result
    message = result["extras"]["exception_message"]
    # reset_password sends a secret, so MEHO withholds its error text.
    if "withheld" not in message:
        assert code in message, message


def _refused_after_wait(op_id: str, result: dict[str, Any], code: str) -> None:
    """The approved run is refused.

    A delete builds its preview again right before it runs, and the preview
    runs the same check, so MEHO refuses a delete there: the new preview
    does not build.
    """
    if op_id == "keycloak.user.delete":
        assert result["status"] == "denied", result
        assert result["extras"]["error_code"] == "preview_changed_since_approval", result
        return
    _refused_on_run(result, code)


#: Words of advice to change rights or to go around the check; no refusal text has them.
_ADVICE = ("give a second person", "ask another", "admin console", "first give", "then send")


def _refused_before_approval(call: dict[str, Any], words: str) -> None:
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "refused_before_approval", call
    assert words in call["error"], call["error"]
    assert "this write was refused:" in call["error"], call["error"]
    assert not [w for w in _ADVICE if w in call["error"].casefold()], call["error"]


async def _approve_and_run(call: dict[str, Any], approver: Operator | None = None) -> Any:
    approver = approver or _operator("kc-access-approver").model_copy(
        update={"platform_admin": True}
    )
    # A change of rights is approved with a click in the web console.
    approver = approver.model_copy(update={"web_session": True})
    request_id = uuid.UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


def test_the_lists_of_guarded_writes() -> None:
    assert {op for op, _ in _LOCK_OUT} == set(LOCK_OUT_WRITES) - {"secret.move"}
    assert {op for op, _ in _SERVICE} == set(SERVICE_LOGIN_WRITES) - {"secret.move"}


# ---------------------------------------------------------------------------
# A login MEHO itself uses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "extra"), _SERVICE, ids=_SERVICE_IDS)
async def test_a_meho_service_login_is_refused_before_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        call = await _call(op_id, _params(op_id, _SERVICE_UUID, extra))
        written = _writes(mock)
    _refused_before_approval(call, "a login that MEHO itself uses")
    assert _SERVICE_LOGIN not in call["error"]
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _SERVICE, ids=_SERVICE_IDS)
async def test_a_meho_service_login_is_refused_when_it_runs(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        result = await _run(op_id, _params(op_id, _SERVICE_UUID, extra))
        written = _writes(mock)
    _refused_on_run(result, "meho_service_login")
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _SERVICE, ids=_SERVICE_IDS)
async def test_a_login_meho_starts_to_use_while_the_request_waits_is_refused_on_run(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    extra: dict[str, Any],
) -> None:
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "another-client")
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        call = await _call(op_id, _params(op_id, _SERVICE_UUID, extra))
        assert call["status"] == "awaiting_approval", call
        # While the request waits, MEHO is set up to use this login.
        monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", _ADMIN_CLIENT)
        get_settings.cache_clear()
        resumed = await _approve_and_run(call)
        written = _writes(mock)
    _refused_after_wait(op_id, resumed.model_dump(mode="json"), "meho_service_login")
    assert written == []


# ---------------------------------------------------------------------------
# The last platform admin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_the_last_platform_admin_is_refused_before_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    assert "No approval and no grant allows this write" in call["error"]
    assert _ADMIN not in call["error"]
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_the_last_platform_admin_is_refused_when_it_runs(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        result = await _run(op_id, _params(op_id, _ADMIN_UUID, extra))
        written = _writes(mock)
    _refused_on_run(result, "last_platform_admin")
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_with_a_second_platform_admin_the_write_waits_for_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
    assert call["status"] == "awaiting_approval", call


#: The writes whose run the mocked Keycloak can finish (a delete is read back, a
#: password is read from Vault).
_RUNS = [(op, extra) for op, extra in _LAST_ADMIN if not op.endswith((".delete", "password"))]


@pytest.mark.parametrize(
    ("op_id", "extra"), _RUNS, ids=[op.removeprefix("keycloak.") for op, _ in _RUNS]
)
async def test_with_a_second_platform_admin_another_admin_approves_the_write(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
        assert call["status"] == "awaiting_approval", call
        request_id = uuid.UUID(call["extras"]["approval_request_id"])
        # An operator cannot approve it.
        async with get_sessionmaker()() as session:
            with pytest.raises(
                (ApprovalGroupForbiddenError, ApprovalRoleFloorError, RightsChangeApprovalError)
            ):
                await approve_request(
                    session,
                    request_id,
                    operator=_operator("made-up-operator", TenantRole.OPERATOR),
                    params=None,
                )
        assert _writes(mock) == []
        resumed = await _approve_and_run(call)
        written = _writes(mock)
    assert resumed.status == "ok", resumed
    assert written, written


@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_a_second_platform_admin_who_leaves_while_the_request_waits_is_counted_on_run(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
        assert call["status"] == "awaiting_approval", call
        # While the request waits, the second platform admin is disabled.
        api.users[_SECOND_ADMIN_UUID]["enabled"] = False
        resumed = await _approve_and_run(call)
        written = _writes(mock)
    _refused_after_wait(op_id, resumed.model_dump(mode="json"), "last_platform_admin")
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_a_member_whose_own_attribute_is_false_is_not_counted(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        # In the platform admin group, but the user's own value wins in the login.
        member = {**_user(_SECOND_ADMIN_UUID, "member-without-rights")}
        member["attributes"] = {"platform_admin": ["false"]}
        api.add(member, _PLATFORM_GROUP)
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
        result = await _run(op_id, _params(op_id, _ADMIN_UUID, extra))
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    assert written == []


async def test_a_member_whose_own_attribute_is_true_is_counted(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        member = {**_user(_SECOND_ADMIN_UUID, "second-platform-admin")}
        member["attributes"] = {"platform_admin": ["true"]}
        api.add(member, _PLATFORM_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
    assert call["status"] == "awaiting_approval", call


async def test_leaving_a_group_that_gives_no_platform_admin_rights_is_not_refused(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.members[_TEAM_GROUP].append(_ADMIN_UUID)
        call = await _call(
            "keycloak.group.member.remove", {"user_id": _ADMIN_UUID, "group_id": _TEAM_GROUP}
        )
    assert call["status"] == "awaiting_approval", call


async def test_a_platform_admin_by_a_group_attribute_counts_too(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.members[_PLATFORM_GROUP] = []
        api.groups[_TEAM_GROUP]["attributes"] = {"platform_admin": ["true"]}
        api.members[_TEAM_GROUP] = [_ADMIN_UUID, _SERVICE_UUID]
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
    # The service login in the same group is not a person, so it is not counted.
    _refused_before_approval(call, "the last active platform admin")


async def test_a_person_who_is_no_platform_admin_is_not_refused(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    other = "77777777-7777-7777-7777-777777777777"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(other, "event-guest"), _TEAM_GROUP)
        call = await _call("keycloak.user.disable", {"id": other})
    assert call["status"] == "awaiting_approval", call


# ---------------------------------------------------------------------------
# The requester's own account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "extra"), _LOCK_OUT, ids=_LOCK_OUT_IDS)
async def test_the_requesters_own_account_is_refused_before_approval(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        call = await _call(op_id, _params(op_id, _REQUESTER_UUID, extra))
        written = _writes(mock)
    _refused_before_approval(call, "the account that asked for this write")
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _LOCK_OUT, ids=_LOCK_OUT_IDS)
async def test_the_requesters_own_account_is_refused_when_it_runs(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        result = await _run(op_id, _params(op_id, _REQUESTER_UUID, extra))
        written = _writes(mock)
    _refused_on_run(result, "own_account")
    assert written == []


@pytest.mark.parametrize(("op_id", "extra"), _LOCK_OUT, ids=_LOCK_OUT_IDS)
async def test_the_account_of_the_person_who_runs_the_approved_request_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    approver_uuid = "88888888-8888-8888-8888-888888888888"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(approver_uuid, "the-approver"), _TEAM_GROUP)
        call = await _call(op_id, _params(op_id, approver_uuid, extra))
        assert call["status"] == "awaiting_approval", call
        # In the login realm a platform admin approves every Keycloak write.
        approver = _operator(approver_uuid).model_copy(update={"platform_admin": True})
        # An admin may approve a change of their own login; the run refuses the lock-out.
        resumed = await _approve_and_run(call, approver)
        written = _writes(mock)
    _refused_after_wait(op_id, resumed.model_dump(mode="json"), "own_account")
    assert written == []


# ---------------------------------------------------------------------------
# A standing grant does not let any of the three through
# ---------------------------------------------------------------------------

_BOT = "svc:access-bot"
_BOT_CLIENT = "access-bot"
_BOT_UUID = "99999999-9999-9999-9999-999999999999"


async def _grant_disable() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub=_BOT,
                op_id="keycloak.user.disable",
                connector_id=_CONNECTOR_ID,
                target_id=(await _target(_LOGIN_REALM_TARGET)).id,
                reason="unattended clean-up",
                created_by_sub="kc-access-admin",
            )
        )
        await session.commit()


def _bot() -> Operator:
    return Operator(
        sub=_BOT,
        name="access bot",
        email=None,
        raw_jwt="<kc-access-bot-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
        client_id=_BOT_CLIENT,
    )


@pytest.mark.parametrize(
    ("user_id", "words"),
    [
        (_SERVICE_UUID, "a login that MEHO itself uses"),
        (_ADMIN_UUID, "the last active platform admin"),
        (_BOT_UUID, "the account that asked for this write"),
    ],
    ids=["service-login", "last-platform-admin", "own-account"],
)
async def test_a_standing_grant_on_disable_lets_none_of_them_through(
    kc: KeycloakConnector,  # noqa: F811
    user_id: str,
    words: str,
) -> None:
    await _grant_disable()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_BOT_UUID, f"service-account-{_BOT_CLIENT}"), _TEAM_GROUP)
        call = await _call("keycloak.user.disable", {"id": user_id}, _bot())
        written = _writes(mock)
    assert call["status"] != "ok", call
    assert words in str(call), call
    assert written == []


async def test_a_standing_grant_on_disable_still_runs_for_another_user(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    other = "77777777-7777-7777-7777-777777777777"
    await _grant_disable()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(other, "event-guest"), _TEAM_GROUP)
        call = await _call("keycloak.user.disable", {"id": other}, _bot())
        # In the login realm a platform admin approves every Keycloak write,
        # also one with a standing grant; the access check lets it through.
        assert call["status"] == "awaiting_approval", call
        result = await _approve_and_run(call)
        written = _writes(mock)
    assert result.status == "ok", result
    assert written == [f"PUT /admin/realms/{_LOGIN_REALM}/users/{other}"]


# ---------------------------------------------------------------------------
# On doubt MEHO refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_a_failed_keycloak_read_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        api.groups_fail = True
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
        result = await _run(op_id, _params(op_id, _ADMIN_UUID, extra))
        written = _writes(mock)
    _refused_before_approval(call, "a Keycloak read failed")
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


def _odd_member_row(api: _Realm) -> None:
    api.extra_members[_PLATFORM_GROUP] = ["not-an-object"]


def _member_row_without_id(api: _Realm) -> None:
    api.extra_members[_PLATFORM_GROUP] = [{"username": "no-id"}]


def _member_with_only_an_id(api: _Realm) -> None:
    only_id = "77777777-7777-7777-7777-777777777777"
    api.users[only_id] = {"id": only_id}
    api.members[_PLATFORM_GROUP].insert(0, only_id)


def _user_group_row_without_id(api: _Realm) -> None:
    api.extra_user_groups = [{"name": "no-id"}]


_ODD_ROWS = [
    _odd_member_row,
    _member_row_without_id,
    _member_with_only_an_id,
    _user_group_row_without_id,
]


@pytest.mark.parametrize("odd", _ODD_ROWS, ids=[f.__name__.strip("_") for f in _ODD_ROWS])
@pytest.mark.parametrize(("op_id", "extra"), _LAST_ADMIN, ids=_LAST_ADMIN_IDS)
async def test_a_row_in_an_odd_shape_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    extra: dict[str, Any],
    odd: Any,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        # Without the odd row, the second platform admin lets the write wait.
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        odd(api)
        call = await _call(op_id, _params(op_id, _ADMIN_UUID, extra))
        result = await _run(op_id, _params(op_id, _ADMIN_UUID, extra))
        written = _writes(mock)
    _refused_before_approval(call, "a Keycloak read failed")
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


async def test_platform_admin_rights_from_a_realm_role_are_refused(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", "meho-platform-admin")
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
    _refused_before_approval(call, "come from a realm role here")


async def test_with_a_realm_role_setting_a_holder_of_the_role_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", "meho-platform-admin")
    get_settings.cache_clear()
    holder = "77777777-7777-7777-7777-777777777777"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(holder, "role-holder"), _TEAM_GROUP)
        api.roles[holder] = [{"id": "made-up-role", "name": "meho-platform-admin"}]
        call = await _call("keycloak.user.disable", {"id": holder})
    _refused_before_approval(call, "come from a realm role here")


async def test_with_a_realm_role_setting_another_person_still_waits(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", "meho-platform-admin")
    get_settings.cache_clear()
    other = "77777777-7777-7777-7777-777777777777"
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(other, "event-guest"), _TEAM_GROUP)
        call = await _call("keycloak.user.disable", {"id": other})
    assert call["status"] == "awaiting_approval", call


# ---------------------------------------------------------------------------
# Another realm is not MEHO's login realm
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user_id", [_SERVICE_UUID, _ADMIN_UUID, _REQUESTER_UUID])
async def test_the_same_users_in_another_realm_are_not_refused(
    kc: KeycloakConnector,  # noqa: F811
    user_id: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock, _OTHER_REALM)
        result = await _run("keycloak.user.disable", {"id": user_id}, _OTHER_REALM_TARGET)
        written = _writes(mock)
    assert result["status"] == "ok", result
    assert written == [f"PUT /admin/realms/{_OTHER_REALM}/users/{user_id}"]
