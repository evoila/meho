# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a caller reads of the platform admin facts, and the ``secret.move`` sides.

* The approval request a person reads always holds the ``grant`` block
  with its facts and reason. With ``KEYCLOAK_RIGHTS_CHANGE_CARDS`` off, the
  agent's own ``preview_operation`` and the MCP approval reads
  (``proposed_effect_for_agent``) give the caller the same output as an
  op without these facts: no ``grant`` block where there was none, and no
  reason and no fact read from the login in the ``grant`` block of the
  other writes of one login or group. With the switch on, the caller reads
  them too.
* A ``secret.move`` whose Keycloak user sink is in a realm named
  ``clients`` is still read as a sink, with a Vault source and with a
  Keycloak client-secret source in another realm.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    REASON_LOGIN,
    REASON_LOGIN_UNREAD,
    REASON_MAPPER,
    REASON_NEW_USER,
    _keycloak_sides,
)
from meho_backplane.operations._preview import build_proposed_effect, proposed_effect_for_agent
from meho_backplane.settings import get_settings
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import _LOGIN_REALMS, _OTHER_USER, _REALM, _ctx, _park
from tests.test_keycloak_login_realm_platform_admin_writes import (
    _CREATE_PARAMS,
    _GROUP_ID,
    _VAULT_REF,
    _connector,
    _effect,
    _Keycloak,
    _login,
    _login_realm,  # noqa: F401 -- autouse fixture: the realm is MEHO's own login realm
    _ops_group,
    _refused_for_a_tenant_admin,
    _scope_mapper,
    _scope_mapper_params,
)

_RESET = "keycloak.user.reset_password"
_UPDATE = "keycloak.user.update"
_SCOPE_MAPPER = "keycloak.client_scope.protocol_mapper.update"
_MEMBER_ADD = "keycloak.group.member.add"
_MOVE_TO_LOGIN = {"from": f"vault:{_VAULT_REF}", "to": f"keycloak:kc/{_REALM}/alice#password"}


@pytest.fixture
def keycloak(monkeypatch: pytest.MonkeyPatch) -> _Keycloak:
    """The fake Keycloak, also for ``secret.move``, which finds its target and connector itself."""
    state = _Keycloak()
    state.connector = _connector(state)

    async def _resolve_target(_session: Any, _tenant: Any, name: str) -> Any:
        return _ctx("secret.move", {}, realm=_REALM).target

    monkeypatch.setattr("meho_backplane.targets.resolver.resolve_target", _resolve_target)
    monkeypatch.setattr(
        "meho_backplane.operations._handler_resolve.get_or_create_connector_instance",
        lambda _cls: state.connector,
    )
    return state


def _cards(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", "true" if on else "false")
    get_settings.cache_clear()


async def _agent_preview(state: _Keycloak, op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    ctx = _ctx(op_id, params, realm=_REALM, connector=state.connector)
    effect = await build_proposed_effect(replace(ctx, agent_preview=True))
    assert effect is not None
    preview = effect["preview"]
    assert isinstance(preview, dict)
    return preview


async def _agent_read(state: _Keycloak, op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """The stored request as a person reads it, and as ``meho_approvals_get`` gives it back."""
    effect = await _effect(state, op_id, params)
    assert "grant" in effect["preview"]
    shown = proposed_effect_for_agent(
        op_id, effect, params=params, connector_id="keycloak-1.x", tenant=None
    )
    assert shown is not None
    preview = shown["preview"]
    assert isinstance(preview, dict)
    return preview


# ---------------------------------------------------------------------------
# The agent's own preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_the_agent_preview_of_a_password_reset_has_no_grant(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)
    _login(keycloak, platform=True)
    params = {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}

    preview = await _agent_preview(keycloak, _RESET, params)

    assert "grant" not in preview


@pytest.mark.asyncio
async def test_switch_off_the_agent_preview_of_a_login_change_reads_no_login(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)
    _login(keycloak, platform=True)
    keycloak.failing = ("/users",)
    params = {"id": _OTHER_USER, "changes": {"email": "a@b.example"}}

    grant = (await _agent_preview(keycloak, _UPDATE, params))["grant"]

    assert grant["platform_admin"] is False
    assert "platform_admin_reason" not in grant


@pytest.mark.asyncio
async def test_switch_off_the_agent_preview_of_a_group_change_has_no_reason(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)
    _ops_group(keycloak, platform=True)
    keycloak.lists[f"/groups/{_GROUP_ID}/members"] = []
    params = {"group_id": _GROUP_ID, "user_id": _OTHER_USER}

    grant = (await _agent_preview(keycloak, _MEMBER_ADD, params))["grant"]

    assert grant["platform_admin"] is True
    assert "platform_admin_reason" not in grant


@pytest.mark.asyncio
async def test_switch_on_the_agent_preview_carries_the_login_facts(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=True)
    _login(keycloak, platform=True)
    params = {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}

    grant = (await _agent_preview(keycloak, _RESET, params))["grant"]

    assert grant["platform_admin"] is True
    assert grant["platform_admin_reason"] == REASON_LOGIN


# ---------------------------------------------------------------------------
# The MCP approval reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_an_approval_read_hides_the_new_grant_blocks(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)
    _ops_group(keycloak, platform=True)
    _login(keycloak, platform=True)
    _scope_mapper(keycloak, claim="platform_admin")
    cases: list[tuple[str, dict[str, Any]]] = [
        ("keycloak.user.create", copy.deepcopy(_CREATE_PARAMS)),
        (_RESET, {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}),
        (_SCOPE_MAPPER, _scope_mapper_params()),
        ("secret.move", dict(_MOVE_TO_LOGIN)),
    ]

    for op_id, params in cases:
        assert "grant" not in await _agent_read(keycloak, op_id, params), op_id


@pytest.mark.asyncio
@pytest.mark.parametrize("unread", [False, True], ids=["platform-login", "unread-login"])
async def test_switch_off_an_approval_read_of_a_login_change_shows_the_old_fact(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch, unread: bool
) -> None:
    _cards(monkeypatch, on=False)
    _login(keycloak, platform=not unread)
    if unread:
        keycloak.failing = ("/groups",)
    params = {"id": _OTHER_USER, "changes": {"email": "a@b.example"}}
    stored = (await _effect(keycloak, _UPDATE, params))["preview"]["grant"]
    assert stored["platform_admin"] is (None if unread else True)
    assert stored["platform_admin_reason"] == (REASON_LOGIN_UNREAD if unread else REASON_LOGIN)

    grant = (await _agent_read(keycloak, _UPDATE, params))["grant"]

    assert grant["platform_admin"] is False
    assert "platform_admin_reason" not in grant


@pytest.mark.asyncio
async def test_switch_off_an_approval_read_of_a_group_change_has_no_reason(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=False)
    _ops_group(keycloak, platform=True)
    keycloak.lists[f"/groups/{_GROUP_ID}/members"] = []
    params = {"group_id": _GROUP_ID, "user_id": _OTHER_USER}

    grant = (await _agent_read(keycloak, _MEMBER_ADD, params))["grant"]

    assert grant["platform_admin"] is True
    assert "platform_admin_reason" not in grant


@pytest.mark.asyncio
async def test_switch_on_an_approval_read_shows_the_grant_blocks(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cards(monkeypatch, on=True)
    _ops_group(keycloak, platform=True)
    _login(keycloak, platform=True)
    _scope_mapper(keycloak, claim="platform_admin")
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("keycloak.user.create", copy.deepcopy(_CREATE_PARAMS), REASON_NEW_USER),
        (_RESET, {"id": _OTHER_USER, "password_secret_ref": _VAULT_REF}, REASON_LOGIN),
        (_UPDATE, {"id": _OTHER_USER, "changes": {"email": "a@b.example"}}, REASON_LOGIN),
        (_SCOPE_MAPPER, _scope_mapper_params(), REASON_MAPPER),
        ("secret.move", dict(_MOVE_TO_LOGIN), REASON_LOGIN),
    ]

    for op_id, params, reason in cases:
        grant = (await _agent_read(keycloak, op_id, params))["grant"]
        assert grant["platform_admin"] is True, op_id
        assert grant["platform_admin_reason"] == reason, op_id


# ---------------------------------------------------------------------------
# secret.move: a sink in a realm named "clients"
# ---------------------------------------------------------------------------

_CLIENTS_SINK = "keycloak:kc/clients/alice#password"
_SOURCES = {
    "vault-source": f"vault:{_VAULT_REF}",
    "keycloak-client-source": "keycloak:kc/other-realm/clients/launcher-app#secret",
}


@pytest.mark.parametrize("source", list(_SOURCES.values()), ids=list(_SOURCES))
def test_a_sink_in_a_realm_named_clients_is_a_sink(source: str) -> None:
    sink, _source_realm = _keycloak_sides({"from": source, "to": _CLIENTS_SINK})

    assert sink == ("kc", "clients", "alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", list(_SOURCES.values()), ids=list(_SOURCES))
async def test_a_move_to_a_platform_login_in_a_realm_named_clients_needs_a_platform_admin(
    keycloak: _Keycloak, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {"clients"})
    _login(keycloak)
    realms: list[str] = []

    async def _has_platform(_c: Any, _ctx: Any, realm: str, user_id: str) -> bool:
        realms.append(realm)
        return user_id == _OTHER_USER

    monkeypatch.setattr(
        "meho_backplane.connectors.keycloak.ops_write_platform_reach.login_has_platform_admin",
        _has_platform,
    )
    params = {"from": source, "to": _CLIENTS_SINK}

    effect = await _effect(keycloak, "secret.move", params)

    grant = effect["preview"]["grant"]
    assert grant["login_realm"] is True
    assert grant["user_id"] == _OTHER_USER
    assert grant["platform_admin"] is True
    assert grant["platform_admin_reason"] == REASON_LOGIN
    assert realms == ["clients"]
    request_id = await _park(params, effect, op_id="secret.move")
    assert REASON_LOGIN in await _refused_for_a_tenant_admin(request_id, params)
