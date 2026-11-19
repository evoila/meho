# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a Keycloak write in the login realm gives decides the reason on its card.

In MEHO's own login realm a platform admin approves every Keycloak write
that sets more than the harmless keys
(``tests/test_keycloak_login_realm_every_write.py``). For each write below,
MEHO also reads what it gives, and the card names the exact reason:

* the write can give platform admin rights, or acts on the login of a
  person who has them: a tenant admin's approval is refused with that
  reason, and a platform admin's approval goes through;
* the write gives none of that: the card names the harmless keys rule;
* the read that decides fails: the card says MEHO could not read it.

The writes: ``keycloak.user.create``, ``keycloak.user.reset_password``,
``keycloak.user.update``, ``keycloak.protocol_mapper.create``,
``keycloak.client_scope.protocol_mapper.update``, ``keycloak.client.update``,
``keycloak.client.create``, ``keycloak.client_scope.create`` and
``keycloak.realm.update``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.ops_write_grant import REASON_GROUP_OR_ROLE
from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    REASON_KEYS,
    REASON_LOGIN,
    REASON_LOGIN_UNREAD,
    REASON_MAPPER,
    REASON_NEW_USER,
    REASON_REALM_DEFAULTS,
    REASON_UNREAD,
    mapper_sets_platform_admin,
)
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations._preview import build_proposed_effect
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError
from meho_backplane.operations.grant_approval import (
    GRANT_REASON_STRICTER,
    grant_changed_since_request,
    grant_facts,
    is_rights_change_op,
    platform_admin_reason,
)
from meho_backplane.settings import get_settings
from meho_backplane.ui.routes.approvals.render import project_request_to_view
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

_GROUP_ID = "77777777-7777-7777-7777-777777777777"
_SCOPE_ID = "88888888-8888-8888-8888-888888888888"
_MAPPER_ID = "99999999-9999-9999-9999-999999999999"
_CLIENT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ROLE = "meho-platform"
_VAULT_REF = "secret/meho/new-login#password"


@dataclass
class _Keycloak:
    """What the fake Keycloak holds, by path below ``/admin/realms/<realm>``."""

    objects: dict[str, Any] = field(default_factory=dict)
    lists: dict[str, list[Any]] = field(default_factory=dict)
    groups_by_name: dict[str, str] = field(default_factory=dict)
    users_by_name: dict[str, str] = field(default_factory=dict)
    failing: tuple[str, ...] = ()
    connector: Any = None

    def read(self, path: str) -> str:
        rel = unquote(path).removeprefix(f"/admin/realms/{_REALM}")
        if any(part in rel for part in self.failing):
            raise RuntimeError("read failed")
        return rel


def _connector(state: _Keycloak) -> KeycloakConnector:
    connector = KeycloakConnector()

    async def _get_admin_json(
        _t: Any, path: str, *, operator: Any, params: Any = None
    ) -> dict[str, Any]:
        rel = state.read(path)
        if rel == "":
            return {"realm": _REALM}
        if rel not in state.objects:
            request = httpx.Request("GET", f"https://kc.invalid{path}")
            raise httpx.HTTPStatusError(
                "not found", request=request, response=httpx.Response(404, request=request)
            )
        return copy.deepcopy(state.objects[rel])

    async def _get_admin_list(
        _t: Any, path: str, *, operator: Any, params: Any = None, strict: bool = False
    ) -> list[Any]:
        return copy.deepcopy(state.lists.get(state.read(path), []))

    async def _find_group(
        _t: Any, _realm: str, name: str, parent_id: str | None, *, operator: Any
    ) -> dict[str, Any] | None:
        state.read(f"/admin/realms/{_REALM}/groups")
        found = state.groups_by_name.get(name)
        return {"id": found, "name": name} if found else None

    async def _find_user(_t: Any, _realm: str, username: str, *, operator: Any) -> str | None:
        return state.users_by_name.get(username)

    async def _find_client(_t: Any, _realm: str, client_id: str, *, operator: Any) -> str | None:
        return _CLIENT_UUID

    connector._get_admin_json = _get_admin_json  # type: ignore[method-assign]
    connector._get_admin_list = _get_admin_list  # type: ignore[method-assign]
    connector._find_group = _find_group  # type: ignore[method-assign]
    connector._find_user_uuid = _find_user  # type: ignore[method-assign]
    connector._find_client_uuid = _find_client  # type: ignore[method-assign]
    return connector


def _ops_group(state: _Keycloak, *, platform: bool = False) -> None:
    attributes = {"platform_admin": ["true"]} if platform else {"team": ["ops"]}
    state.objects[f"/groups/{_GROUP_ID}"] = {
        "id": _GROUP_ID,
        "name": "ops",
        "path": "/ops",
        "attributes": attributes,
    }
    state.groups_by_name["ops"] = _GROUP_ID


def _login(state: _Keycloak, *, platform: bool = False) -> None:
    attributes = {"platform_admin": ["true"]} if platform else {}
    state.objects[f"/users/{_OTHER_USER}"] = {
        "id": _OTHER_USER,
        "username": "alice",
        "attributes": attributes,
    }
    state.users_by_name["alice"] = _OTHER_USER


def _scope_mapper(state: _Keycloak, *, claim: str = "team") -> None:
    state.objects[f"/client-scopes/{_SCOPE_ID}"] = {"id": _SCOPE_ID, "name": "meho"}
    state.objects[f"/client-scopes/{_SCOPE_ID}/protocol-mappers/models/{_MAPPER_ID}"] = {
        "id": _MAPPER_ID,
        "name": "a-claim",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {"claim.name": claim, "claim.value": "x"},
    }


def _set_platform_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", _ROLE)
    get_settings.cache_clear()


def _role_mapper() -> dict[str, Any]:
    """A mapper that adds the realm role ``meho-platform`` to every token."""
    return {
        "name": "give-role",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-role-mapper",
        "config": {"role": _ROLE},
    }


def _claim_mapper(claim: str) -> dict[str, Any]:
    return {
        "name": "a-claim",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {"claim.name": claim, "claim.value": "true"},
    }


def _settings_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings that name the platform admin claim and role cannot be read."""

    def _broken() -> Any:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.ops_write_effective_roles.get_settings", _broken
    )


@dataclass(frozen=True)
class _Case:
    """One write: its params, and how to set up each kind of Keycloak state.

    Each setup gets the fake Keycloak and the monkeypatch.
    """

    op_id: str
    reason: str
    platform: Callable[[_Keycloak, pytest.MonkeyPatch], dict[str, Any]]
    normal: Callable[[_Keycloak, pytest.MonkeyPatch], dict[str, Any]]
    unread: Callable[[_Keycloak, pytest.MonkeyPatch], dict[str, Any]]


_CREATE_PARAMS = {
    "representation": {"username": "new-login", "groups": ["/ops"]},
    "password_secret_ref": _VAULT_REF,
}


def _user_create(state: _Keycloak, mp: pytest.MonkeyPatch, *, platform: bool) -> dict[str, Any]:
    _ops_group(state, platform=platform)
    return copy.deepcopy(_CREATE_PARAMS)


def _user_create_unread(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
    _ops_group(state)
    state.failing = ("/groups",)
    return copy.deepcopy(_CREATE_PARAMS)


def _login_case(op_id: str, params: dict[str, Any]) -> _Case:
    def _setup(state: _Keycloak, mp: pytest.MonkeyPatch, *, platform: bool) -> dict[str, Any]:
        _login(state, platform=platform)
        return copy.deepcopy(params)

    def _unread(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        _login(state)
        state.failing = ("/groups",)
        return copy.deepcopy(params)

    return _Case(
        op_id,
        REASON_LOGIN,
        lambda s, mp: _setup(s, mp, platform=True),
        lambda s, mp: _setup(s, mp, platform=False),
        _unread,
    )


def _mapper_case(op_id: str, wrap: Callable[[dict[str, Any]], dict[str, Any]]) -> _Case:
    """A write whose params carry a mapper; *wrap* puts the mapper into the params."""

    def _platform(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        return wrap(_claim_mapper("platform_admin"))

    def _normal(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        return wrap(_role_mapper())

    def _unread(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        _settings_fail(mp)
        return wrap(_role_mapper())

    return _Case(op_id, REASON_MAPPER, _platform, _normal, _unread)


def _scope_mapper_params() -> dict[str, Any]:
    return {
        "scope_id": _SCOPE_ID,
        "mapper_id": _MAPPER_ID,
        "representation": {"config": {"claim.value": "y"}},
    }


def _scope_mapper_case() -> _Case:
    def _platform(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        _scope_mapper(state, claim="platform_admin")
        return _scope_mapper_params()

    def _normal(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        _scope_mapper(state)
        return _scope_mapper_params()

    def _unread(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        _scope_mapper(state)
        state.failing = ("/protocol-mappers/",)
        return _scope_mapper_params()

    return _Case(
        "keycloak.client_scope.protocol_mapper.update",
        REASON_MAPPER,
        _platform,
        _normal,
        _unread,
    )


def _realm_update_case() -> _Case:
    params = {"representation": {"defaultGroups": ["/ops"]}}

    def _setup(state: _Keycloak, mp: pytest.MonkeyPatch, *, platform: bool) -> dict[str, Any]:
        _ops_group(state, platform=platform)
        return copy.deepcopy(params)

    def _unread(state: _Keycloak, mp: pytest.MonkeyPatch) -> dict[str, Any]:
        _ops_group(state)
        state.failing = ("/groups",)
        return copy.deepcopy(params)

    return _Case(
        "keycloak.realm.update",
        REASON_REALM_DEFAULTS,
        lambda s, mp: _setup(s, mp, platform=True),
        lambda s, mp: _setup(s, mp, platform=False),
        _unread,
    )


_CASES = [
    _Case(
        "keycloak.user.create",
        REASON_NEW_USER,
        lambda s, mp: _user_create(s, mp, platform=True),
        lambda s, mp: _user_create(s, mp, platform=False),
        _user_create_unread,
    ),
    _login_case(
        "keycloak.user.reset_password", {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}
    ),
    _login_case("keycloak.user.update", {"id": _OTHER_USER, "changes": {"email": "a@b.example"}}),
    _mapper_case(
        "keycloak.protocol_mapper.create",
        lambda mapper: {"client_id": "meho-console", "representation": mapper},
    ),
    _scope_mapper_case(),
    _mapper_case(
        "keycloak.client.update",
        lambda mapper: {
            "client_id": "meho-console",
            "representation": {"protocolMappers": [mapper]},
        },
    ),
    _mapper_case(
        "keycloak.client.create",
        lambda mapper: {"representation": {"clientId": "new-app", "protocolMappers": [mapper]}},
    ),
    _mapper_case(
        "keycloak.client_scope.create",
        lambda mapper: {"representation": {"name": "new-scope", "protocolMappers": [mapper]}},
    ),
    _realm_update_case(),
]
_IDS = [case.op_id for case in _CASES]


@pytest.fixture(autouse=True)
def _login_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    monkeypatch.delenv("JWT_PLATFORM_ADMIN_ROLE_NAME", raising=False)
    get_settings.cache_clear()


@pytest.fixture
def keycloak(monkeypatch: pytest.MonkeyPatch) -> _Keycloak:
    """The fake Keycloak, also for ``secret.move``, which finds its target and connector itself."""
    state = _Keycloak()
    connector = _connector(state)

    async def _resolve_target(_session: Any, _tenant: Any, name: str) -> Any:
        return _ctx("secret.move", {}, realm=_REALM).target

    monkeypatch.setattr("meho_backplane.targets.resolver.resolve_target", _resolve_target)
    monkeypatch.setattr(
        "meho_backplane.operations._handler_resolve.get_or_create_connector_instance",
        lambda _cls: connector,
    )
    state.connector = connector
    return state


async def _effect(state: _Keycloak, op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    ctx = _ctx(op_id, params, realm=_REALM, connector=state.connector)
    effect = await build_proposed_effect(ctx)
    assert effect is not None
    return effect


def _grant(effect: dict[str, Any]) -> dict[str, Any]:
    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is True
    return grant


async def _refused_for_a_tenant_admin(request_id: Any, params: dict[str, Any]) -> str:
    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value
    return exc.value.reason_text


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_a_tenant_admin_is_refused_and_a_platform_admin_approves(
    case: _Case, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = case.platform(keycloak, monkeypatch)
    effect = await _effect(keycloak, case.op_id, params)

    grant = _grant(effect)
    assert grant["platform_admin"] is True
    assert grant["platform_admin_reason"] == case.reason
    request_id = await _park(params, effect, op_id=case.op_id)

    reason = await _refused_for_a_tenant_admin(request_id, params)
    assert "platform admin must approve" in reason
    assert case.reason.rstrip(".") in reason

    await _approve(request_id, _person("person-platform", platform_admin=True), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_a_write_that_gives_no_platform_admin_rights_names_the_harmless_keys_rule(
    case: _Case, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = case.normal(keycloak, monkeypatch)
    effect = await _effect(keycloak, case.op_id, params)

    grant = _grant(effect)
    assert grant["platform_admin"] is True
    assert grant["platform_admin_reason"] == REASON_KEYS
    request_id = await _park(params, effect, op_id=case.op_id)
    reason = await _refused_for_a_tenant_admin(request_id, params)
    assert REASON_KEYS.rstrip(".") in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=_IDS)
async def test_a_failed_read_refuses_a_tenant_admin(
    case: _Case, keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = case.unread(keycloak, monkeypatch)
    effect = await _effect(keycloak, case.op_id, params)

    grant = _grant(effect)
    assert grant["platform_admin"] is None
    unread = REASON_LOGIN_UNREAD if case.reason == REASON_LOGIN else REASON_UNREAD
    assert grant["platform_admin_reason"] == unread
    request_id = await _park(params, effect, op_id=case.op_id)
    reason = await _refused_for_a_tenant_admin(request_id, params)
    assert "could not read" in reason


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_login_got_platform_admin_rights_while_it_waited(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change of harmless keys needs a tenant admin until the login gets platform admin rights."""
    _login(keycloak)
    params = {"id": _OTHER_USER, "changes": {"firstName": "Alice"}}
    effect = await _effect(keycloak, "keycloak.user.update", params)
    assert _grant(effect)["platform_admin"] is False
    request_id = await _park(params, effect, op_id="keycloak.user.update")
    await _approve(request_id, _person("person-admin"), params)
    keycloak.lists[f"/users/{_OTHER_USER}/groups"] = [
        {"id": _GROUP_ID, "name": "oncall", "path": "/MEHO-Platform-Admins/oncall"}
    ]

    async def _live_card(operator: Operator, **kwargs: Any) -> dict[str, Any]:
        return await _effect(keycloak, kwargs["op_id"], kwargs["params"])

    monkeypatch.setattr("meho_backplane.operations.approval_object_check.live_card", _live_card)

    reason = await grant_changed_since_request(
        _person(_REQUESTER), await _row(request_id), None, params
    )

    assert reason == GRANT_REASON_STRICTER


# ---------------------------------------------------------------------------
# More shapes of the same rule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "representation",
    [
        {"username": "new-login", "attributes": {"platform_admin": ["true"]}},
        {"username": "new-login", "groups": ["/MEHO-Platform-Admins/oncall"]},
    ],
    ids=["attribute", "under-platform-group"],
)
async def test_a_new_login_with_platform_admin_content_needs_a_platform_admin(
    keycloak: _Keycloak, representation: dict[str, Any]
) -> None:
    effect = await _effect(keycloak, "keycloak.user.create", {"representation": representation})

    assert _grant(effect)["platform_admin"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "representation",
    [
        {"username": "new-login", "realmRoles": ["viewer"]},
        {"username": "new-login", "clientRoles": {"app": ["viewer"]}},
        {"username": "new-login", "groups": ["/no-such-group"]},
    ],
    ids=["realm-roles", "client-roles", "unknown-group"],
)
async def test_a_new_login_meho_cannot_judge_needs_a_platform_admin(
    keycloak: _Keycloak, representation: dict[str, Any]
) -> None:
    effect = await _effect(keycloak, "keycloak.user.create", {"representation": representation})

    assert _grant(effect)["platform_admin"] is None


@pytest.mark.asyncio
async def test_a_login_with_the_platform_admin_role_needs_a_platform_admin(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_platform_role(monkeypatch)
    _login(keycloak)
    keycloak.lists[f"/users/{_OTHER_USER}/role-mappings/realm/composite"] = [
        {"id": "role-1", "name": "viewer"},
        {"id": "role-2", "name": _ROLE},
    ]
    params = {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}

    effect = await _effect(keycloak, "keycloak.user.reset_password", params)

    assert _grant(effect)["platform_admin"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mapper",
    [
        _claim_mapper(" Platform_Admin "),
        {"name": "s", "protocolMapper": "script-anything.js", "config": {}},
        {"name": "s", "protocolMapper": "oidc-script-based-protocol-mapper", "config": {}},
    ],
    ids=["claim-name", "script", "script-based"],
)
async def test_a_mapper_that_can_set_the_claim_needs_a_platform_admin(
    keycloak: _Keycloak, mapper: dict[str, Any]
) -> None:
    params = {"client_id": "meho-console", "representation": mapper}

    effect = await _effect(keycloak, "keycloak.protocol_mapper.create", params)

    assert _grant(effect)["platform_admin"] is True


@pytest.mark.parametrize(
    ("claim", "sets"),
    [
        ("meho\\.platform_admin", True),
        ("meho.platform_admin", True),
        ("meho\\.team", False),
    ],
)
def test_a_claim_name_with_escaped_dots_counts_as_the_plain_name(
    monkeypatch: pytest.MonkeyPatch, claim: str, sets: bool
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_CLAIM_NAME", "meho.platform_admin")
    get_settings.cache_clear()

    assert mapper_sets_platform_admin(_claim_mapper(claim)) is sets


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["defaultRole", "defaultRoles", "users", "clients"])
async def test_a_realm_update_of_defaults_or_parts_needs_a_platform_admin(
    keycloak: _Keycloak, key: str
) -> None:
    params = {"representation": {key: {"name": "x"} if key == "defaultRole" else ["x"]}}

    effect = await _effect(keycloak, "keycloak.realm.update", params)

    assert _grant(effect)["platform_admin"] is True


@pytest.mark.asyncio
async def test_a_realm_update_of_the_login_realm_from_another_target_counts(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``realm`` param names the realm that changes, not the target's managed realm."""
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {"login"})
    params = {"realm": "login", "representation": {"defaultRoles": ["x"]}}

    effect = await _effect(keycloak, "keycloak.realm.update", params)

    assert _grant(effect)["login_realm"] is True
    assert effect["preview"]["grant"]["platform_admin"] is True


@pytest.mark.asyncio
async def test_outside_the_login_realm_meho_reads_nothing_more(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {"other"})
    keycloak.failing = ("/groups",)

    effect = await _effect(keycloak, "keycloak.user.create", copy.deepcopy(_CREATE_PARAMS))

    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is False
    assert grant["platform_admin"] is False


@pytest.mark.asyncio
async def test_a_secret_move_between_vault_places_is_not_a_grant() -> None:
    params = {"from": "vault:secret/a#x", "to": "vault:secret/b#x"}
    request_id = await _park(params, None, op_id="secret.move")

    assert grant_facts(await _row(request_id)) is None
    assert not is_rights_change_op("secret.move")


@pytest.mark.asyncio
async def test_the_approval_card_says_why_a_platform_admin_must_approve(
    keycloak: _Keycloak,
) -> None:
    _login(keycloak, platform=True)
    params = {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}
    effect = await _effect(keycloak, "keycloak.user.reset_password", params)
    request_id = await _park(params, effect, op_id="keycloak.user.reset_password")

    view = project_request_to_view(await _row(request_id))

    assert view["platform_admin_reason"] == REASON_LOGIN
    assert platform_admin_reason(await _row(request_id)) == REASON_LOGIN


@pytest.mark.asyncio
async def test_a_group_change_card_names_its_reason(keycloak: _Keycloak) -> None:
    _ops_group(keycloak, platform=True)
    params = {"group_id": _GROUP_ID, "user_id": _OTHER_USER}
    keycloak.lists[f"/groups/{_GROUP_ID}/members"] = []

    effect = await _effect(keycloak, "keycloak.group.member.add", params)

    assert effect["preview"]["grant"]["platform_admin_reason"] == REASON_GROUP_OR_ROLE


@pytest.mark.asyncio
async def test_the_agent_preview_carries_no_grant_block(keycloak: _Keycloak) -> None:
    from dataclasses import replace

    ctx = _ctx(
        "keycloak.user.create",
        copy.deepcopy(_CREATE_PARAMS),
        realm=_REALM,
        connector=keycloak.connector,
    )

    effect = await build_proposed_effect(replace(ctx, agent_preview=True))

    assert effect is not None
    assert "grant" not in effect["preview"]
