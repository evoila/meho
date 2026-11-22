# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""In MEHO's own login realm, a platform admin approves every Keycloak write.

The one exception is a write that sets only keys of ``HARMLESS_KEYS``
(display names and descriptions). For each Keycloak write, and for
``secret.move`` with a Keycloak source or sink:

* in the login realm, a tenant admin's approval is refused;
* a platform admin's approval goes through, and the run check passes;
* when MEHO cannot read which realms its own login uses, the write counts
  as one in the login realm, and a tenant admin's approval is refused;
* a write in another realm needs only a tenant admin; when its realm
  becomes a login realm while the request waits, the run is refused.

A request approved with a card that has no harmless keys fact does not
run, in every realm. Fixture values are made up.
"""

from __future__ import annotations

import copy
import importlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write_login_realm_rule import (
    HARMLESS_KEYS,
    sets_only_harmless_keys,
)
from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    REASON_KEYS,
    REASON_SECRET_MOVE,
)
from meho_backplane.connectors.keycloak.rights_changes import (
    NOT_RIGHTS_CHANGE_OPS,
    RIGHTS_CHANGE_OPS,
)
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations._preview import build_proposed_effect
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError
from meho_backplane.operations.grant_approval import (
    GRANT_REASON_OLD_CARD,
    GRANT_REASON_STRICTER,
    HARMLESS_KEYS_ONLY,
    grant_changed_since_request,
    is_rights_change_op,
)
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import (
    _LOGIN_REALMS,
    _OTHER_USER,
    _REALM,
    _REQUESTER,
    _approve,
    _ctx,
    _park,
    _person,
    _row,
    _status,
)

# Loading this module adds the approval card of the required actions write.
importlib.import_module("meho_backplane.connectors.keycloak.ops_user_required_actions")

_OTHER_REALM = "team-realm"
_GROUP_ID = "77777777-7777-7777-7777-777777777777"
_SCOPE_ID = "88888888-8888-8888-8888-888888888888"
_MAPPER_ID = "99999999-9999-9999-9999-999999999999"
_CLIENT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_VAULT = "vault:secret/meho/example#password"
_REALM_DELETE = "keycloak.realm.delete"


class _FakeKeycloak:
    """A Keycloak that answers every read with a plain object or an empty list."""

    def __init__(self) -> None:
        self.realm = _REALM
        #: The name Keycloak answers with for a realm read by another name.
        self.realm_answers: dict[str, str] = {}
        #: The realms whose read fails, and the realms Keycloak does not know.
        self.realm_reads_fail: set[str] = set()
        self.missing_realms: set[str] = set()

    def read_realm(self, path: str) -> dict[str, Any] | None:
        """The answer to ``GET /admin/realms/{realm}``; ``None`` for another path."""
        head, _sep, name = path.rstrip("/").rpartition("/")
        if not head.endswith("/admin/realms"):
            return None
        name = unquote(name)
        if name in self.realm_reads_fail:
            raise RuntimeError("Keycloak is not reachable")
        if name in self.missing_realms:
            request = httpx.Request("GET", f"https://keycloak.example{path}")
            raise httpx.HTTPStatusError(
                "not found", request=request, response=httpx.Response(404, request=request)
            )
        return {"realm": self.realm_answers.get(name, name), "enabled": True}

    def connector(self) -> KeycloakConnector:
        connector = KeycloakConnector()
        state = self

        async def _get_admin_json(
            _t: Any, path: str, *, operator: Any, params: Any = None
        ) -> dict[str, Any]:
            if (realm := state.read_realm(path)) is not None:
                return realm
            if path.endswith("/clients"):
                return []  # type: ignore[return-value]
            if "/roles/" in path:
                return {
                    "id": "role-client",
                    "name": path.rsplit("/", 1)[-1],
                    "clientRole": True,
                    "containerId": _CLIENT_UUID,
                    "composite": False,
                }
            if "/protocol-mappers/models/" in path:
                return {
                    "id": _MAPPER_ID,
                    "name": "a-claim",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-hardcoded-claim-mapper",
                    "config": {"claim.name": "team", "claim.value": "x"},
                }
            return {
                "id": path.rsplit("/", 1)[-1],
                "name": "example",
                "path": "/example",
                "username": "alice",
                "clientId": "example-app",
                "enabled": True,
                "attributes": {},
            }

        async def _get_admin_list(
            _t: Any, path: str, *, operator: Any, params: Any = None, strict: bool = False
        ) -> list[Any]:
            if path.endswith("/credentials"):
                return [{"id": "cred-1", "type": "otp"}]
            return []

        async def _find_realm_role(
            _t: Any, _realm: str, name: str, *, operator: Any
        ) -> dict[str, Any]:
            return {"id": f"role-{name}", "name": name, "composite": False}

        async def _find_group(
            _t: Any, _realm: str, name: str, parent_id: str | None, *, operator: Any
        ) -> dict[str, Any] | None:
            return {"id": _GROUP_ID, "name": name, "path": f"/{name}", "attributes": {}}

        async def _find_user(_t: Any, _realm: str, username: str, *, operator: Any) -> str:
            return _OTHER_USER

        async def _find_client(_t: Any, _realm: str, client_id: str, *, operator: Any) -> str:
            return _CLIENT_UUID

        connector._get_admin_json = _get_admin_json  # type: ignore[method-assign]
        connector._get_admin_list = _get_admin_list  # type: ignore[method-assign]
        connector._find_group = _find_group  # type: ignore[method-assign]
        connector._find_user_uuid = _find_user  # type: ignore[method-assign]
        connector._find_client_uuid = _find_client  # type: ignore[method-assign]
        connector._find_realm_role = _find_realm_role  # type: ignore[method-assign]
        return connector


@dataclass(frozen=True)
class _Write:
    op_id: str
    #: The params, with ``{realm}`` for the realm the write changes.
    params: dict[str, Any]
    #: The realm the target manages, when it is not the realm the write changes.
    target_realm: str | None = None


def _fill(value: Any, realm: str) -> Any:
    if isinstance(value, str):
        return value.replace("{realm}", realm)
    if isinstance(value, dict):
        return {key: _fill(item, realm) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, realm) for item in value]
    return value


_USER = {"id": _OTHER_USER}

_WRITES: list[_Write] = [
    _Write("keycloak.authflow.ensure_approval_flow", {"client_id": "meho-console"}),
    _Write("keycloak.authflow.ensure_approval_touch", {"client_id": "meho-console"}),
    _Write("keycloak.authflow.ensure_passkey_code_skip", {}),
    _Write("keycloak.client.create", {"representation": {"clientId": "new-app"}}),
    _Write("keycloak.client.delete", {"client_id": "old-app"}),
    _Write(
        "keycloak.client.update",
        {"client_id": "meho-console", "representation": {"redirectUris": ["https://x.example/*"]}},
    ),
    _Write("keycloak.client_scope.create", {"representation": {"name": "new-scope"}}),
    _Write(
        "keycloak.client_scope.protocol_mapper.update",
        {
            "scope_id": _SCOPE_ID,
            "mapper_id": _MAPPER_ID,
            "representation": {"config": {"claim.value": "y"}},
        },
    ),
    _Write("keycloak.group.create", {"name": "ops"}),
    _Write("keycloak.group.delete", {"id": _GROUP_ID, "delete_subgroups": True}),
    _Write("keycloak.group.member.add", {"group_id": _GROUP_ID, "user_id": _OTHER_USER}),
    _Write("keycloak.group.member.remove", {"group_id": _GROUP_ID, "user_id": _OTHER_USER}),
    _Write("keycloak.group.update_attributes", {"id": _GROUP_ID, "attributes": {"team": ["a"]}}),
    _Write(
        "keycloak.protocol_mapper.create",
        {
            "client_id": "meho-console",
            "representation": {
                "name": "a-claim",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-hardcoded-claim-mapper",
                "config": {"claim.name": "team", "claim.value": "x"},
            },
        },
    ),
    _Write("keycloak.realm.create", {"representation": {"realm": "{realm}"}}),
    _Write(_REALM_DELETE, {"realm": "{realm}"}, target_realm="kc-home"),
    _Write(
        "keycloak.realm.update",
        {"realm": "{realm}", "representation": {"smtpServer": {"host": "mail.example"}}},
    ),
    _Write("keycloak.role_mapping.assign", {**_USER, "roles": ["viewer"]}),
    _Write("keycloak.role_mapping.client.assign", {**_USER, "client_id": "app", "roles": ["v"]}),
    _Write("keycloak.role_mapping.client.remove", {**_USER, "client_id": "app", "roles": ["v"]}),
    _Write("keycloak.user.check_login", {"username": "alice", "password_secret_ref": _VAULT}),
    _Write("keycloak.user.create", {"representation": {"username": "new-login"}}),
    _Write("keycloak.user.credential.delete", {**_USER, "credential_type": "otp"}),
    _Write("keycloak.user.delete", dict(_USER)),
    _Write("keycloak.user.disable", dict(_USER)),
    _Write("keycloak.user.enable", dict(_USER)),
    _Write("keycloak.user.logout", dict(_USER)),
    _Write("keycloak.user.required_actions.update", {**_USER, "add": ["CONFIGURE_TOTP"]}),
    _Write("keycloak.user.reset_password", {**_USER, "password_secret_ref": _VAULT}),
    _Write("keycloak.user.update", {**_USER, "changes": {"email": "a@b.example"}}),
    _Write("secret.move", {"from": _VAULT, "to": "keycloak:kc/{realm}/alice#password"}),
    _Write("secret.move", {"from": "keycloak:kc/{realm}/clients/app#secret", "to": _VAULT}),
]


def _id(write: _Write) -> str:
    if write.op_id != "secret.move":
        return write.op_id
    return "secret.move-sink" if "keycloak:" in str(write.params["to"]) else "secret.move-source"


_IDS = [_id(write) for write in _WRITES]
#: MEHO never deletes the login realm, so that write never runs there.
_RUNS = [write for write in _WRITES if write.op_id != _REALM_DELETE]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeKeycloak:
    """The fake Keycloak, also for ``secret.move``, which finds its target and connector itself."""
    keycloak = _FakeKeycloak()
    _login_realms(monkeypatch, {_REALM})

    async def _resolve_target(_session: Any, _tenant: Any, _name: str) -> Any:
        return _ctx("secret.move", {}, realm=keycloak.realm).target

    monkeypatch.setattr("meho_backplane.targets.resolver.resolve_target", _resolve_target)
    monkeypatch.setattr(
        "meho_backplane.operations._handler_resolve.get_or_create_connector_instance",
        lambda _cls: keycloak.connector(),
    )
    return keycloak


def _login_realms(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: set(names))
    get_settings.cache_clear()


async def _effect(fake: _FakeKeycloak, write: _Write, realm: str) -> tuple[dict[str, Any], Any]:
    fake.realm = write.target_realm or realm
    params = _fill(copy.deepcopy(write.params), realm)
    ctx = _ctx(write.op_id, params, realm=fake.realm, connector=fake.connector())
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect, params


async def _run_check(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeKeycloak, request_id: Any, write: _Write, realm: str
) -> str | None:
    async def _live_card(operator: Any, **kwargs: Any) -> dict[str, Any]:
        effect, _params = await _effect(fake, write, realm)
        return effect

    monkeypatch.setattr("meho_backplane.operations.approval_object_check.live_card", _live_card)
    params = _fill(copy.deepcopy(write.params), realm)
    return await grant_changed_since_request(
        _person(_REQUESTER), await _row(request_id), None, params
    )


async def _tenant_admin_refused(request_id: Any, params: dict[str, Any]) -> str:
    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    return exc.value.reason_text


def test_every_keycloak_write_is_in_these_tests() -> None:
    assert {write.op_id for write in _WRITES} == {
        *RIGHTS_CHANGE_OPS,
        *NOT_RIGHTS_CHANGE_OPS,
        "secret.move",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _WRITES, ids=_IDS)
async def test_in_the_login_realm_a_tenant_admin_is_refused(
    write: _Write, fake: _FakeKeycloak
) -> None:
    effect, params = await _effect(fake, write, _REALM)
    request_id = await _park(params, effect, op_id=write.op_id)

    reason = await _tenant_admin_refused(request_id, params)

    assert "platform admin must approve" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _RUNS, ids=[_id(write) for write in _RUNS])
async def test_in_the_login_realm_a_platform_admin_approves_and_it_runs(
    write: _Write, fake: _FakeKeycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect, params = await _effect(fake, write, _REALM)
    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is True
    assert grant[HARMLESS_KEYS_ONLY] is False
    assert grant["platform_admin"] is not False
    request_id = await _park(params, effect, op_id=write.op_id)

    await _approve(request_id, _person("person-platform", platform_admin=True), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert await _run_check(monkeypatch, fake, request_id, write, _REALM) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _WRITES, ids=_IDS)
async def test_when_meho_cannot_read_its_login_realms_a_tenant_admin_is_refused(
    write: _Write, fake: _FakeKeycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken() -> set[str]:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(_LOGIN_REALMS, _broken)
    effect, params = await _effect(fake, write, _OTHER_REALM)
    request_id = await _park(params, effect, op_id=write.op_id)

    reason = await _tenant_admin_refused(request_id, params)

    assert "platform admin must approve" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _WRITES, ids=_IDS)
async def test_in_another_realm_a_tenant_admin_approves_and_a_new_login_realm_stops_the_run(
    write: _Write, fake: _FakeKeycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect, params = await _effect(fake, write, _OTHER_REALM)
    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is False
    assert grant["platform_admin"] is False
    request_id = await _park(params, effect, op_id=write.op_id)
    await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert await _run_check(monkeypatch, fake, request_id, write, _OTHER_REALM) is None

    _login_realms(monkeypatch, {_REALM, _OTHER_REALM})

    reason = await _run_check(monkeypatch, fake, request_id, write, _OTHER_REALM)
    assert reason is not None
    if write.op_id != _REALM_DELETE:
        assert reason == GRANT_REASON_STRICTER


# ---------------------------------------------------------------------------
# The harmless keys
# ---------------------------------------------------------------------------


_HARMLESS: list[_Write] = [
    _Write("keycloak.realm.update", {"realm": "{realm}", "representation": {"displayName": "M"}}),
    _Write(
        "keycloak.client.update",
        {"client_id": "meho-console", "representation": {"name": "C", "description": "D"}},
    ),
    _Write("keycloak.user.update", {**_USER, "changes": {"firstName": "A", "lastName": "B"}}),
]


def test_the_harmless_list_names_three_writes() -> None:
    assert set(HARMLESS_KEYS) == {write.op_id for write in _HARMLESS}


@pytest.mark.asyncio
@pytest.mark.parametrize("write", _HARMLESS, ids=[write.op_id for write in _HARMLESS])
async def test_in_the_login_realm_only_a_platform_admin_approves_a_write_of_harmless_keys(
    write: _Write, fake: _FakeKeycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect, params = await _effect(fake, write, _REALM)
    grant = effect["preview"]["grant"]
    assert grant[HARMLESS_KEYS_ONLY] is True
    assert grant["platform_admin"] is False
    request_id = await _park(params, effect, op_id=write.op_id)

    with pytest.raises(ApprovalRoleFloorError):
        await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, _person("person-platform", platform_admin=True), params)

    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value
    assert await _run_check(monkeypatch, fake, request_id, write, _REALM) is None


@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("keycloak.realm.update", {"representation": {"displayName": "M", "loginTheme": "x"}}),
        ("keycloak.realm.update", {"representation": {"displayName": "M", "realm": "x"}}),
        ("keycloak.realm.update", {"representation": {}}),
        (
            "keycloak.realm.update",
            {"representation": {"displayName": "M"}, "smtp_password_secret_ref": "a/b"},
        ),
        ("keycloak.client.update", {"representation": {"name": "C", "redirectUris": ["/"]}}),
        ("keycloak.client.update", {"representation": {"name": "C", "secret": "x"}}),
        ("keycloak.user.update", {"id": _OTHER_USER, "changes": {"firstName": "A", "email": "e"}}),
        ("keycloak.user.update", {"id": _OTHER_USER, "changes": {"username": "root"}}),
        ("keycloak.user.update", {"id": _OTHER_USER, "changes": "firstName"}),
        ("keycloak.user.enable", {"id": _OTHER_USER}),
        ("keycloak.group.create", {"name": "ops"}),
    ],
)
def test_a_key_that_is_not_on_the_list_counts_as_strict(op_id: str, params: dict[str, Any]) -> None:
    assert sets_only_harmless_keys(op_id, params) is False


@pytest.mark.asyncio
async def test_the_card_says_why_a_platform_admin_must_approve(fake: _FakeKeycloak) -> None:
    write = next(write for write in _WRITES if write.op_id == "keycloak.user.enable")
    effect, params = await _effect(fake, write, _REALM)
    assert effect["preview"]["grant"]["platform_admin_reason"] == REASON_KEYS
    request_id = await _park(params, effect, op_id=write.op_id)

    reason = await _tenant_admin_refused(request_id, params)

    assert REASON_KEYS.rstrip(".") in reason


@pytest.mark.asyncio
async def test_a_secret_move_card_says_why(fake: _FakeKeycloak) -> None:
    write = next(write for write in _WRITES if write.op_id == "secret.move")

    effect, _params = await _effect(fake, write, _REALM)

    assert effect["preview"]["grant"]["platform_admin_reason"] == REASON_SECRET_MOVE


# ---------------------------------------------------------------------------
# Requests approved before the rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("realm", [_REALM, _OTHER_REALM])
@pytest.mark.parametrize("write", _RUNS, ids=[_id(write) for write in _RUNS])
async def test_a_request_approved_with_an_older_card_does_not_run(
    write: _Write, realm: str, fake: _FakeKeycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect, params = await _effect(fake, write, realm)
    del effect["preview"]["grant"][HARMLESS_KEYS_ONLY]
    effect["preview"]["grant"]["platform_admin"] = False
    request_id = await _park(params, effect, op_id=write.op_id)
    await _approve(request_id, _person("person-platform", platform_admin=True), params)

    reason = await _run_check(monkeypatch, fake, request_id, write, realm)

    assert reason == GRANT_REASON_OLD_CARD


@pytest.mark.parametrize("op_id", sorted(NOT_RIGHTS_CHANGE_OPS))
def test_a_write_that_only_takes_away_keeps_its_approval_shape(op_id: str) -> None:
    """It is checked in the login realm, but stays no change of rights for grants elsewhere."""
    assert not is_rights_change_op(op_id)
