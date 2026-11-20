# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Keycloak group change that can give platform admin rights needs a platform admin.

MEHO decides this from what the group gives, not from its name:

* the ``platform_admin=true`` attribute on the group or a parent group;
* a place under the platform admin group (``/MEHO-Platform-Admins/...``);
* the realm role in ``JWT_PLATFORM_ADMIN_ROLE_NAME``, when it is set;
* for a group attribute change: new attributes that set ``platform_admin``.

A group MEHO cannot read counts as the strict case. Right before an
approved change runs, MEHO reads the group again with the same rule.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.models import ApprovalRequestStatus
from meho_backplane.operations._preview import build_proposed_effect
from meho_backplane.operations.approval_queue import ApprovalRoleFloorError
from meho_backplane.operations.grant_approval import (
    GRANT_REASON_STRICTER,
    grant_changed_since_request,
)
from meho_backplane.settings import get_settings
from tests._keycloak_access_guard_helpers import platform_admins_not_counted
from tests.test_api_v1_approvals_fresh_sign_in import (
    _settings_env,  # noqa: F401 -- autouse fixture: pins the env these tests need
)
from tests.test_approval_grant_to_self import (
    _ADD,
    _ATTRS,
    _GROUP,
    _LOGIN_REALMS,
    _OTHER_USER,
    _PLATFORM_GROUP,
    _PLATFORM_NAME,
    _REALM,
    _REQUESTER,
    _approve,
    _connector_with,
    _ctx,
    _groups_by_id,
    _park,
    _person,
    _row,
    _status,
)

#: Writes in MEHO's login realm count the platform admins; these tests are about other checks.
pytestmark = pytest.mark.usefixtures(platform_admins_not_counted.__name__)


_PARENT = "66666666-6666-6666-6666-666666666666"
_ROLE = "meho-platform"
_ADD_PARAMS: dict[str, Any] = {"group_id": _GROUP, "user_id": _OTHER_USER}


def _ops_team(**attributes: list[str]) -> dict[str, Any]:
    return {"name": "ops-team", "path": "/ops-team", "attributes": dict(attributes)}


def _connector(
    *, roles: list[dict[str, Any]] | None = None, roles_fail: bool = False
) -> KeycloakConnector:
    """A connector for the login realm: the group has no members and gives *roles*."""
    connector, _ = _connector_with(members={_GROUP: []})
    member_list = connector._get_admin_list

    async def _get_admin_list(
        target: Any, path: str, *, operator: Any, params: Any = None, strict: bool = False
    ) -> list[Any]:
        if path.endswith("/role-mappings/realm/composite"):
            if roles_fail:
                raise RuntimeError("read failed")
            return roles or []
        return await member_list(target, path, operator=operator, params=params, strict=strict)

    connector._get_admin_list = _get_admin_list  # type: ignore[method-assign]
    return connector


async def _effect(
    connector: KeycloakConnector | None, params: dict[str, Any], op_id: str = _ADD
) -> dict[str, Any]:
    effect = await build_proposed_effect(_ctx(op_id, params, realm=_REALM, connector=connector))
    assert effect is not None
    return effect


async def _needs_platform_admin(
    effect: dict[str, Any], params: dict[str, Any], op_id: str = _ADD
) -> None:
    """A tenant admin who is not a platform admin is refused; a platform admin approves."""
    request_id = await _park(params, effect, op_id=op_id)

    with pytest.raises(ApprovalRoleFloorError) as exc:
        await _approve(request_id, _person("person-admin"), params)
    assert "platform admin" in exc.value.reason_text
    assert await _status(request_id) == ApprovalRequestStatus.PENDING.value

    await _approve(request_id, _person("person-platform", platform_admin=True), params)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.fixture(autouse=True)
def _login_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_LOGIN_REALMS, lambda: {_REALM})
    monkeypatch.delenv("JWT_PLATFORM_ADMIN_ROLE_NAME", raising=False)
    get_settings.cache_clear()


def _set_platform_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_ROLE_NAME", _ROLE)
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [["true"], ["TRUE"], ["false", "true"]])
async def test_a_group_with_the_platform_admin_attribute_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch, value: list[str]
) -> None:
    _groups_by_id(monkeypatch, {_GROUP: _ops_team(platform_admin=value)})

    effect = await _effect(_connector(), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is True
    await _needs_platform_admin(effect, _ADD_PARAMS)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/MEHO-Platform-Admins/oncall", "/meho-platform-admins/a/b"])
async def test_a_subgroup_under_the_platform_admin_group_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    group = {"name": "oncall", "path": path, "parentId": _PLATFORM_GROUP, "attributes": {}}
    _groups_by_id(monkeypatch, {_GROUP: group})

    effect = await _effect(_connector(), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is True
    await _needs_platform_admin(effect, _ADD_PARAMS)


@pytest.mark.asyncio
async def test_a_group_whose_parent_has_the_attribute_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = {"name": "oncall", "path": "/ops/oncall", "parentId": _PARENT, "attributes": {}}
    parent = {"name": "ops", "path": "/ops", "attributes": {"platform_admin": ["true"]}}
    _groups_by_id(monkeypatch, {_GROUP: child, _PARENT: parent})

    effect = await _effect(_connector(), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is True
    await _needs_platform_admin(effect, _ADD_PARAMS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attributes",
    [{"platform_admin": ["true"]}, {"Platform_Admin ": "true"}, {"platform_admin": ["false"]}],
)
async def test_an_attribute_change_that_sets_platform_admin_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch, attributes: dict[str, Any]
) -> None:
    _groups_by_id(monkeypatch, {_GROUP: _ops_team()})
    params = {"id": _GROUP, "attributes": attributes}

    effect = await _effect(_connector(), params, op_id=_ATTRS)

    assert effect["preview"]["grant"]["platform_admin"] is True
    await _needs_platform_admin(effect, params, op_id=_ATTRS)


@pytest.mark.asyncio
async def test_a_group_that_gives_the_platform_admin_role_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_platform_role(monkeypatch)
    _groups_by_id(monkeypatch, {_GROUP: _ops_team()})
    roles = [{"id": "role-1", "name": "viewer"}, {"id": "role-2", "name": _ROLE}]

    effect = await _effect(_connector(roles=roles), _ADD_PARAMS)
    other = await _effect(_connector(roles=roles[:1]), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is True
    assert other["preview"]["grant"]["platform_admin"] is False
    await _needs_platform_admin(effect, _ADD_PARAMS)


@pytest.mark.asyncio
async def test_the_role_is_not_read_when_no_platform_admin_role_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _groups_by_id(monkeypatch, {_GROUP: _ops_team()})

    effect = await _effect(_connector(roles_fail=True), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("groups", "roles_fail", "role_set"),
    [
        ({}, False, False),
        ({_GROUP: {"name": "oncall", "path": "/ops/oncall", "parentId": _PARENT}}, False, False),
        ({_GROUP: {"name": "loop", "path": "/loop", "parentId": _GROUP}}, False, False),
        ({_GROUP: _ops_team()}, True, True),
        ({_GROUP: _ops_team()}, False, True),
    ],
    ids=["group-unreadable", "parent-unreadable", "parent-loop", "roles-unreadable", "bad-role"],
)
async def test_a_group_meho_cannot_read_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch,
    groups: dict[str, Any],
    roles_fail: bool,
    role_set: bool,
) -> None:
    if role_set:
        _set_platform_role(monkeypatch)
    _groups_by_id(monkeypatch, groups)
    roles = None if roles_fail else [{"id": "role-1"}]

    effect = await _effect(_connector(roles=roles, roles_fail=roles_fail), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is None
    await _needs_platform_admin(effect, _ADD_PARAMS)


@pytest.mark.asyncio
async def test_a_group_too_deep_to_read_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"00000000-0000-0000-0000-{n:012d}" for n in range(30)]
    chain: dict[str, Any] = {
        group_id: {"name": f"g{n}", "path": f"/g{n}", "parentId": parent}
        for n, (group_id, parent) in enumerate(pairwise(ids))
    }
    chain[ids[-1]] = {"name": "top", "path": "/top"}
    _groups_by_id(monkeypatch, {_GROUP: {"name": "leaf", "parentId": ids[0]}, **chain})

    effect = await _effect(_connector(), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is None


@pytest.mark.asyncio
async def test_a_role_that_differs_only_in_letter_case_needs_only_a_tenant_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_platform_role(monkeypatch)
    _groups_by_id(monkeypatch, {_GROUP: _ops_team()})
    roles = [{"id": "role-2", "name": _ROLE.swapcase()}]

    effect = await _effect(_connector(roles=roles), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is False
    request_id = await _park(_ADD_PARAMS, effect)
    await _approve(request_id, _person("person-admin"), _ADD_PARAMS)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
@pytest.mark.parametrize("attributes", [{}, {"platform_admin": ["false"]}, {"team": ["ops"]}])
async def test_a_normal_group_needs_only_a_tenant_admin(
    monkeypatch: pytest.MonkeyPatch, attributes: dict[str, list[str]]
) -> None:
    _set_platform_role(monkeypatch)
    _groups_by_id(monkeypatch, {_GROUP: _ops_team(**attributes)})

    effect = await _effect(_connector(roles=[{"id": "role-1", "name": "viewer"}]), _ADD_PARAMS)

    assert effect["preview"]["grant"]["platform_admin"] is False
    request_id = await _park(_ADD_PARAMS, effect)
    await _approve(request_id, _person("person-admin"), _ADD_PARAMS)
    assert await _status(request_id) == ApprovalRequestStatus.APPROVED.value


@pytest.mark.asyncio
async def test_the_run_is_refused_when_the_group_gets_the_attribute_after_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run reads the group again: it now gives platform admin, so it needs more."""
    _groups_by_id(monkeypatch, {_GROUP: _ops_team()})
    request_id = await _park(_ADD_PARAMS, await _effect(_connector(), _ADD_PARAMS))
    await _approve(request_id, _person("person-admin"), _ADD_PARAMS)
    _groups_by_id(monkeypatch, {_GROUP: _ops_team(platform_admin=["true"])})

    async def _live_card(operator: Operator, **kwargs: Any) -> dict[str, Any]:
        return await _effect(_connector(), kwargs["params"], op_id=kwargs["op_id"])

    import meho_backplane.operations.approval_object_check as object_check_module

    monkeypatch.setattr(object_check_module, "live_card", _live_card)

    reason = await grant_changed_since_request(
        _person(_REQUESTER), await _row(request_id), None, _ADD_PARAMS
    )

    assert reason == GRANT_REASON_STRICTER


@pytest.mark.asyncio
async def test_the_platform_admin_group_by_name_still_needs_a_platform_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _groups_by_id(monkeypatch, {_PLATFORM_GROUP: {"name": _PLATFORM_NAME, "path": "/x"}})
    params = {"group_id": _PLATFORM_GROUP, "user_id": _OTHER_USER}

    effect = await _effect(_connector(), params)

    assert effect["preview"]["grant"]["platform_admin"] is True
