# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keycloak changes of rights show the object as it is now, and the run checks it.

The approval cards of these writes show the object as it is now, read from
Keycloak when the request waits: ``keycloak.role_mapping.assign``,
``keycloak.role_mapping.client.assign``, ``keycloak.group.member.add``,
``keycloak.user.reset_password``, ``keycloak.user.enable`` and the three
login flow writes. Right before the approved run, MEHO reads it again: when
it changed, or cannot be read, the run is refused.

A guard checks that every Keycloak change of rights shows the object as it
is now, or makes a new object.

Fixture values are made up.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import meho_backplane.connectors.keycloak.ops_write_current_state as current_state
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.keycloak import (  # loading the package registers the ops
    KeycloakConnector,
)
from meho_backplane.connectors.keycloak.rights_changes import (
    CURRENT_STATE_ON_CARD,
    NEW_OBJECT_OPS,
    RIGHTS_CHANGE_OPS,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import approval_object_check
from meho_backplane.operations._preview import (
    PreviewContext,
    build_proposed_effect,
    proposed_effect_for_agent,
)
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    refuse_resume_if_preview_changed,
)
from meho_backplane.operations.approval_rerun import _object_changed
from meho_backplane.operations.approval_state_pin import (
    REASON_STATE_CHANGED,
    REASON_STATE_INCOMPLETE,
    REASON_STATE_UNREADABLE,
    state_changed_since_request,
    state_pin_key,
)
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    required_settings_env,  # noqa: F401 -- autouse fixture: pins the env get_settings needs
)

_REALM = "example"
_USER = "11111111-1111-1111-1111-111111111111"
_MEMBER = "22222222-2222-2222-2222-222222222222"
_GROUP = "33333333-3333-3333-3333-333333333333"
_CLIENT = "44444444-4444-4444-4444-444444444444"
_FLOW = "55555555-5555-5555-5555-555555555555"
_SECRET = "made-up-password-9d41"
_BASE = f"/admin/realms/{_REALM}"


@pytest.fixture(autouse=True)
def _cards_switch(
    required_settings_env: None,  # noqa: F811 -- runs first, so get_settings has its env
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYCLOAK_RIGHTS_CHANGE_CARDS", raising=False)
    get_settings.cache_clear()


def _state() -> dict[str, Any]:
    """What the fake Keycloak answers, by path."""
    return {
        f"{_BASE}": {"realm": _REALM, "browserFlow": "browser"},
        f"{_BASE}/users/{_USER}": {
            "id": _USER,
            "username": "example-user",
            "enabled": False,
            "requiredActions": ["CONFIGURE_TOTP"],
            "credentials": [{"type": "password", "value": _SECRET}],
            "attributes": {"note": [_SECRET]},
        },
        f"{_BASE}/users/{_USER}/role-mappings": {
            "realmMappings": [{"name": "viewer"}, {"name": "default-roles-example"}],
            "clientMappings": {
                "example-app": {"client": "example-app", "mappings": [{"name": "reader"}]}
            },
        },
        f"{_BASE}/groups/{_GROUP}": {"id": _GROUP, "name": "example-team"},
        f"{_BASE}/groups/{_GROUP}/role-mappings": {
            "realmMappings": [{"name": "operator"}],
            "clientMappings": {
                "example-app": {"client": "example-app", "mappings": [{"name": "writer"}]}
            },
        },
        f"{_BASE}/groups/{_GROUP}/members": [{"id": _MEMBER, "username": "member-one"}],
        f"{_BASE}/clients/{_CLIENT}": {
            "id": _CLIENT,
            "clientId": "meho-web",
            "authenticationFlowBindingOverrides": {"browser": _FLOW},
        },
        f"{_BASE}/authentication/flows": [
            {"id": _FLOW, "alias": "example login flow"},
            {"id": "66666666-6666-6666-6666-666666666666", "alias": "browser"},
        ],
    }


def _connector(state: dict[str, Any], *, fails: bool = False) -> KeycloakConnector:
    """A Keycloak connector whose admin reads answer from *state*."""

    def _answer(path: str) -> Any:
        if fails:
            raise RuntimeError("made-up read failure")
        return copy.deepcopy(state[path])

    async def _get_json(target: Any, path: str, **kwargs: Any) -> Any:
        return _answer(path)

    async def _get_list(
        target: Any, path: str, *, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        rows = _answer(path)
        if params and "first" in params:
            first, size = int(params["first"]), int(params["max"])
            return rows[first : first + size]
        return rows

    connector = KeycloakConnector()
    connector._get_admin_json = AsyncMock(side_effect=_get_json)  # type: ignore[method-assign]
    connector._get_admin_list = AsyncMock(side_effect=_get_list)  # type: ignore[method-assign]
    connector._find_client_uuid = AsyncMock(return_value=_CLIENT)  # type: ignore[method-assign]

    async def _token(target: Any, operator: Any) -> str:
        if fails:
            raise RuntimeError("made-up sign-in failure")
        connector._admin_tokens[(str(target.tenant_id), str(target.id))] = AsyncMock()
        return "made-up-admin-token"

    connector._admin_token = AsyncMock(side_effect=_token)  # type: ignore[method-assign]
    return connector


@dataclass
class _FakeDescriptor:
    op_id: str


@dataclass
class _FakeTarget:
    extras: dict[str, Any] = field(default_factory=lambda: {"managed_realm": _REALM})
    tenant_id: str = "00000000-0000-0000-0000-00000000c0de"
    id: str = "77777777-7777-7777-7777-777777777777"


def _operator() -> Operator:
    return Operator(
        sub="person-requester",
        raw_jwt="<test-raw-jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-00000000c0de"),
        tenant_role=TenantRole.TENANT_ADMIN,
    )


#: op_id -> params of one call.
_PARAMS: dict[str, dict[str, Any]] = {
    "keycloak.role_mapping.assign": {"id": _USER, "roles": ["reporting"]},
    "keycloak.role_mapping.client.assign": {
        "id": _USER,
        "client_id": "example-app",
        "roles": ["editor"],
    },
    "keycloak.group.member.add": {"user_id": _USER, "group_id": _GROUP},
    "keycloak.user.reset_password": {"id": _USER, "password_secret_ref": "example/path"},
    "keycloak.user.enable": {"id": _USER},
    "keycloak.authflow.ensure_passkey_code_skip": {},
    "keycloak.authflow.ensure_approval_flow": {"client_id": "meho-cli-approve"},
    "keycloak.authflow.ensure_approval_touch": {},
}


def _ctx(op_id: str, connector: Any, *, for_agent: bool = False) -> PreviewContext:
    return PreviewContext(
        descriptor=_FakeDescriptor(op_id=op_id),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_FakeTarget(),
        params=_PARAMS[op_id],
        connector_id="keycloak-admin-26.x",
        agent_preview=for_agent,
    )


async def _effect(op_id: str, connector: Any, *, for_agent: bool = False) -> dict[str, Any]:
    effect = await build_proposed_effect(_ctx(op_id, connector, for_agent=for_agent))
    assert effect is not None
    return effect


async def _card(op_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = (await _effect(op_id, _connector(state or _state())))["preview"]
    return card


# ---------------------------------------------------------------------------
# The card shows the object as it is now
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_assign_shows_the_realm_roles_the_user_has_now() -> None:
    card = await _card("keycloak.role_mapping.assign")

    assert card["current_roles_available"] is True
    assert card["current_roles"] == ["default-roles-example", "viewer"]
    assert card["granted_roles"] == ["reporting"]


@pytest.mark.asyncio
async def test_client_role_assign_shows_the_roles_of_that_client_now() -> None:
    card = await _card("keycloak.role_mapping.client.assign")

    assert card["current_roles_available"] is True
    assert card["current_roles"] == ["reader"]
    assert card["roles"] == ["editor"]


@pytest.mark.asyncio
async def test_client_role_assign_shows_no_roles_for_a_client_the_user_has_none_of() -> None:
    state = _state()
    state[f"{_BASE}/users/{_USER}/role-mappings"].pop("clientMappings")

    card = await _card("keycloak.role_mapping.client.assign", state)

    assert card["current_roles_available"] is True
    assert card["current_roles"] == []


@pytest.mark.asyncio
async def test_group_member_add_shows_the_members_and_the_roles_of_the_group() -> None:
    card = await _card("keycloak.group.member.add")

    assert card["current_group_available"] is True
    assert card["current_group"] == {
        "id": _GROUP,
        "members": [{"id": _MEMBER, "username": "member-one"}],
        "members_complete": True,
        "realm_roles": ["operator"],
        "client_roles": {"example-app": ["writer"]},
    }


@pytest.mark.asyncio
async def test_group_member_add_reads_every_page_of_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(current_state, "_MEMBER_PAGE", 2)
    state = _state()
    members = [{"id": f"member-{i}", "username": f"member-{i}"} for i in range(5)]
    state[f"{_BASE}/groups/{_GROUP}/members"] = members

    card = await _card("keycloak.group.member.add", state)

    assert [m["id"] for m in card["current_group"]["members"]] == [m["id"] for m in members]
    assert card["current_group"]["members_complete"] is True


@pytest.mark.asyncio
async def test_a_group_with_more_members_than_the_cap_says_the_list_is_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(current_state, "_MEMBER_PAGE", 2)
    monkeypatch.setattr(current_state, "MEMBER_CAP", 3)
    state = _state()
    state[f"{_BASE}/groups/{_GROUP}/members"] = [
        {"id": f"member-{i}", "username": f"member-{i}"} for i in range(6)
    ]

    card = await _card("keycloak.group.member.add", state)

    assert len(card["current_group"]["members"]) == 3
    assert card["current_group"]["members_complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", ["keycloak.user.reset_password", "keycloak.user.enable"])
async def test_user_writes_show_the_user_as_stored_and_never_a_credential(op_id: str) -> None:
    card = await _card(op_id)

    assert card["current_user_available"] is True
    assert card["current_user"] == {
        "username": "example-user",
        "enabled": False,
        "required_actions": ["CONFIGURE_TOTP"],
    }
    assert _SECRET not in json.dumps(card)


@pytest.mark.asyncio
async def test_passkey_flow_write_shows_the_flow_the_realm_has_now() -> None:
    card = await _card("keycloak.authflow.ensure_passkey_code_skip")

    assert card["current_binding_available"] is True
    assert card["current_binding"] == {"realm_browser_flow": "browser"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_id", ["keycloak.authflow.ensure_approval_flow", "keycloak.authflow.ensure_approval_touch"]
)
async def test_client_flow_writes_show_the_flow_the_client_has_now(op_id: str) -> None:
    card = await _card(op_id)

    assert card["current_binding_available"] is True
    assert card["current_binding"] == {
        "realm_browser_flow": "browser",
        "client_browser_flow_id": _FLOW,
        "client_browser_flow": "example login flow",
    }


@pytest.mark.asyncio
async def test_a_client_with_no_flow_of_its_own_shows_none() -> None:
    state = _state()
    state[f"{_BASE}/clients/{_CLIENT}"].pop("authenticationFlowBindingOverrides")

    card = await _card("keycloak.authflow.ensure_approval_touch", state)

    assert card["current_binding"]["client_browser_flow_id"] is None
    assert card["current_binding"]["client_browser_flow"] is None


# ---------------------------------------------------------------------------
# A failed or odd read says so
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
async def test_a_failed_read_says_the_state_is_not_available(op_id: str) -> None:
    key = current_state.PINNED_STATE_OPS[op_id]

    card = (await _effect(op_id, _connector(_state(), fails=True)))["preview"]

    assert card[f"{key}_available"] is False
    assert key not in card


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "path", "answer"),
    [
        ("keycloak.role_mapping.assign", f"/users/{_USER}/role-mappings", {"realmMappings": {}}),
        (
            "keycloak.role_mapping.client.assign",
            f"/users/{_USER}/role-mappings",
            {"clientMappings": []},
        ),
        ("keycloak.group.member.add", f"/groups/{_GROUP}/members", [{"id": _MEMBER}]),
        ("keycloak.group.member.add", f"/groups/{_GROUP}/role-mappings", {"realmMappings": [1]}),
        ("keycloak.user.enable", f"/users/{_USER}", {"id": _USER, "username": "example-user"}),
        ("keycloak.authflow.ensure_passkey_code_skip", "", {"realm": _REALM}),
        (
            "keycloak.authflow.ensure_approval_touch",
            f"/clients/{_CLIENT}",
            {"id": _CLIENT, "authenticationFlowBindingOverrides": "browser"},
        ),
    ],
)
async def test_an_odd_answer_says_the_state_is_not_available(
    op_id: str, path: str, answer: Any
) -> None:
    state = _state()
    state[f"{_BASE}{path}"] = answer
    key = current_state.PINNED_STATE_OPS[op_id]

    card = await _card(op_id, state)

    assert card[f"{key}_available"] is False
    assert key not in card


@pytest.mark.asyncio
async def test_no_live_connection_says_the_state_is_not_available() -> None:
    card = (await _effect("keycloak.role_mapping.assign", None))["preview"]

    assert card["current_roles_available"] is False


# ---------------------------------------------------------------------------
# The switch: what the caller sees
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
async def test_switch_off_the_agent_preview_has_no_state(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_off(monkeypatch)
    key = current_state.PINNED_STATE_OPS[op_id]

    effect = await _effect(op_id, _connector(_state()), for_agent=True)

    assert key not in effect["preview"]
    assert f"{key}_available" not in effect["preview"]


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
async def test_switch_off_the_person_still_sees_the_state_and_the_mcp_tools_do_not(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_off(monkeypatch)
    key = current_state.PINNED_STATE_OPS[op_id]

    effect = await _effect(op_id, _connector(_state()))
    shown = proposed_effect_for_agent(
        op_id, effect, params=_PARAMS[op_id], connector_id="keycloak-admin-26.x", tenant=None
    )

    assert effect["preview"][f"{key}_available"] is True
    assert shown is not None
    assert key not in shown["preview"]
    assert f"{key}_available" not in shown["preview"]


# ---------------------------------------------------------------------------
# Right before the run
# ---------------------------------------------------------------------------


def _change_roles(state: dict[str, Any]) -> None:
    state[f"{_BASE}/users/{_USER}/role-mappings"]["realmMappings"].append({"name": "admin"})


def _change_client_roles(state: dict[str, Any]) -> None:
    mappings = state[f"{_BASE}/users/{_USER}/role-mappings"]["clientMappings"]
    mappings["example-app"]["mappings"].append({"name": "admin"})


def _change_members(state: dict[str, Any]) -> None:
    state[f"{_BASE}/groups/{_GROUP}/members"].append({"id": "new-member", "username": "new"})


def _change_user(state: dict[str, Any]) -> None:
    state[f"{_BASE}/users/{_USER}"]["requiredActions"] = []


def _change_realm_flow(state: dict[str, Any]) -> None:
    state[_BASE]["browserFlow"] = "another flow"


def _change_client_flow(state: dict[str, Any]) -> None:
    state[f"{_BASE}/clients/{_CLIENT}"]["authenticationFlowBindingOverrides"] = {}


_CHANGES: dict[str, Callable[[dict[str, Any]], None]] = {
    "keycloak.role_mapping.assign": _change_roles,
    "keycloak.role_mapping.client.assign": _change_client_roles,
    "keycloak.group.member.add": _change_members,
    "keycloak.user.reset_password": _change_user,
    "keycloak.user.enable": _change_user,
    "keycloak.authflow.ensure_passkey_code_skip": _change_realm_flow,
    "keycloak.authflow.ensure_approval_flow": _change_client_flow,
    "keycloak.authflow.ensure_approval_touch": _change_client_flow,
}


def _live_effect(monkeypatch: pytest.MonkeyPatch, effect: dict[str, Any] | None) -> list[str]:
    """Answer the live card with *effect*; return the op_ids it was built for."""
    built: list[str] = []

    async def _fake(operator: Any, *, op_id: str, **kwargs: Any) -> dict[str, Any] | None:
        built.append(op_id)
        return effect

    monkeypatch.setattr(approval_object_check, "live_card", _fake)
    return built


def _request(op_id: str, effect: dict[str, Any] | None) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), op_id=op_id, connector_id="keycloak-admin-26.x", proposed_effect=effect
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
async def test_the_run_goes_ahead_when_the_object_is_as_the_approver_saw_it(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = await _effect(op_id, _connector(_state()))
    _live_effect(monkeypatch, await _effect(op_id, _connector(_state())))

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
async def test_the_run_is_refused_when_the_object_changed_while_the_request_waited(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = await _effect(op_id, _connector(_state()))
    changed = _state()
    _CHANGES[op_id](changed)
    _live_effect(monkeypatch, await _effect(op_id, _connector(changed)))

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason == REASON_STATE_CHANGED
    assert "send the request again" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
async def test_the_run_is_refused_when_the_object_cannot_be_read_now(
    op_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    approved = await _effect(op_id, _connector(_state()))
    _live_effect(monkeypatch, await _effect(op_id, _connector(_state(), fails=True)))

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason == REASON_STATE_UNREADABLE


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_live_card_does_not_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id = "keycloak.role_mapping.assign"
    approved = await _effect(op_id, _connector(_state()))
    _live_effect(monkeypatch, None)

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason == REASON_STATE_UNREADABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approved",
    [
        {"preview": {"current_roles_available": False}},
        {"preview": {"granted_roles": ["reporting"]}},
        None,
    ],
    ids=["not-read-when-it-waited", "made-before-the-card-had-it", "no-card"],
)
async def test_the_run_is_refused_when_the_approver_did_not_see_the_state(
    approved: dict[str, Any] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    op_id = "keycloak.role_mapping.assign"
    built = _live_effect(monkeypatch, await _effect(op_id, _connector(_state())))

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason == REASON_STATE_UNREADABLE
    assert built == []


def _big_group(monkeypatch: pytest.MonkeyPatch, last: str) -> dict[str, Any]:
    """A group with one member more than the cap; *last* is the id of the one beyond it."""
    monkeypatch.setattr(current_state, "_MEMBER_PAGE", 2)
    monkeypatch.setattr(current_state, "MEMBER_CAP", 3)
    state = _state()
    members = [{"id": f"member-{i}", "username": f"member-{i}"} for i in range(3)]
    state[f"{_BASE}/groups/{_GROUP}/members"] = [*members, {"id": last, "username": last}]
    return state


@pytest.mark.asyncio
async def test_the_run_is_refused_when_a_member_beyond_the_cap_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id = "keycloak.group.member.add"
    approved = await _effect(op_id, _connector(_big_group(monkeypatch, "member-x")))
    live = await _effect(op_id, _connector(_big_group(monkeypatch, "member-y")))
    built = _live_effect(monkeypatch, live)
    # Both cards are the same: the replaced member is beyond the cap.
    assert approved["preview"]["current_group"] == live["preview"]["current_group"]

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason == REASON_STATE_INCOMPLETE
    assert built == []


@pytest.mark.asyncio
async def test_a_complete_group_that_grew_past_the_cap_counts_as_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id = "keycloak.group.member.add"
    monkeypatch.setattr(current_state, "_MEMBER_PAGE", 2)
    monkeypatch.setattr(current_state, "MEMBER_CAP", 3)
    approved = await _effect(op_id, _connector(_state()))
    _live_effect(monkeypatch, await _effect(op_id, _connector(_big_group(monkeypatch, "m-x"))))

    reason = await state_changed_since_request(
        _operator(), _request(op_id, approved), None, _PARAMS[op_id]
    )

    assert reason == REASON_STATE_CHANGED


@pytest.mark.asyncio
async def test_an_operation_without_a_pin_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    built = _live_effect(monkeypatch, None)

    reason = await state_changed_since_request(
        _operator(), _request("keycloak.user.disable", None), None, {"id": _USER}
    )

    assert reason is None
    assert built == []


async def _parked(op_id: str, effect: dict[str, Any]) -> ApprovalRequest:
    params = _PARAMS[op_id]
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=_operator(),
            connector_id="keycloak-admin-26.x",
            op_id=op_id,
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect=effect,
        )
        await session.commit()
        request_id = request.id
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_both_run_paths_refuse_a_changed_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """The approve and resume path stores the refusal; the re-run path refuses too."""
    op_id = "keycloak.user.reset_password"
    request = await _parked(op_id, await _effect(op_id, _connector(_state())))
    changed = _state()
    _change_user(changed)
    _live_effect(monkeypatch, await _effect(op_id, _connector(changed)))

    assert await _object_changed(_operator(), request, None, _PARAMS[op_id]) is True
    refusal = await refuse_resume_if_preview_changed(_operator(), request, None, _PARAMS[op_id])

    assert refusal is not None
    assert refusal.status == "denied"
    assert refusal.extras is not None
    assert refusal.extras["error_code"] == "object_changed_since_approval"
    assert refusal.extras["reason"] == REASON_STATE_CHANGED


# ---------------------------------------------------------------------------
# Guard: every change of rights shows the object as it is now
# ---------------------------------------------------------------------------


def test_every_change_of_rights_shows_the_object_now_or_makes_a_new_one() -> None:
    """A new change of rights fails here until its card shows the object as it is now."""
    shown = set(CURRENT_STATE_ON_CARD)
    new = set(NEW_OBJECT_OPS)

    assert not shown & new
    assert shown | new == set(RIGHTS_CHANGE_OPS)


@pytest.mark.parametrize("op_id", sorted(current_state.PINNED_STATE_OPS))
def test_each_state_shown_by_this_change_is_checked_before_the_run(op_id: str) -> None:
    pinned = current_state.PINNED_STATE_OPS[op_id]
    assert state_pin_key(op_id) == pinned == CURRENT_STATE_ON_CARD[op_id]
