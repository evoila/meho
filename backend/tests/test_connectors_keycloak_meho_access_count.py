# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How MEHO counts its platform admins before a write in its own login realm.

Uses the mocked Keycloak of ``test_connectors_keycloak_meho_access_guard.py``
(made-up names and ids). What is proven:

* two writes on the last two platform admins that run at the same moment do
  not both pass: MEHO holds one lock per login realm from the count until
  the write is done;
* a person counts only when MEHO's login gives them the rights: the user's
  own value, else the nearest group value up the parent chain; the name of a
  group gives no rights, and a value with white space around ``true`` makes
  no one another platform admin;
* ``keycloak.group.delete`` and ``keycloak.group.update_attributes`` are
  refused before approval and when they run, when they take the rights of
  the last active platform admin, also when a group without a value gets
  the value ``false``; a failed read refuses them too;
* in a realm with more than 1,000 groups the count reads every page, and a
  person who is no platform admin needs no read of the whole group tree;
* a standing grant on ``keycloak.group.member.remove`` does not let the last
  platform admin leave the platform admin group.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
import respx

from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ServicePrincipalGrant
from meho_backplane.settings import get_settings
from tests.test_connectors_keycloak_meho_access_guard import (  # noqa: F401  (fixtures)
    _ADMIN_UUID,
    _BOT_CLIENT,
    _BOT_UUID,
    _CONNECTOR_ID,
    _KC_BASE_URL,
    _LOGIN_REALM,
    _LOGIN_REALM_TARGET,
    _PLATFORM_GROUP,
    _SECOND_ADMIN_UUID,
    _TEAM_GROUP,
    _TENANT_ID,
    _admin_client_env,
    _bot,
    _call,
    _realm,
    _refused_before_approval,
    _refused_on_run,
    _required_settings_env,
    _reset_module_state,
    _run,
    _stored_requests,
    _target,
    _user,
    _writes,
    kc,
)

_CHILD_GROUP = "aaaaaaaa-0000-0000-0000-000000000001"
_OTHER_ADMIN_GROUP = "aaaaaaaa-0000-0000-0000-000000000002"
_NAME_ONLY_GROUP = "aaaaaaaa-0000-0000-0000-000000000003"
_GUEST_UUID = "77777777-7777-7777-7777-777777777777"


# ---------------------------------------------------------------------------
# Two writes at the same moment
# ---------------------------------------------------------------------------


async def test_two_disables_of_the_last_two_platform_admins_at_once_do_not_both_pass(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _PLATFORM_GROUP)
        api.slow = True
        first, second = await asyncio.gather(
            _run("keycloak.user.disable", {"id": _ADMIN_UUID}),
            _run("keycloak.user.disable", {"id": _SECOND_ADMIN_UUID}),
        )
        written = _writes(mock)
    results = [first, second]
    assert [r["status"] for r in results].count("ok") == 1, results
    refused = next(r for r in results if r["status"] != "ok")
    _refused_on_run(refused, "last_platform_admin")
    assert len(written) == 1, written
    still_enabled = [api.users[uid]["enabled"] for uid in (_ADMIN_UUID, _SECOND_ADMIN_UUID)]
    assert still_enabled.count(True) == 1, still_enabled


# ---------------------------------------------------------------------------
# Count only real platform admins
# ---------------------------------------------------------------------------


async def test_a_child_group_with_its_own_value_false_gives_no_rights(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_CHILD_GROUP, "no-rights", "false", parent=_PLATFORM_GROUP)
        api.add(_user(_SECOND_ADMIN_UUID, "child-member"), _CHILD_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        result = await _run("keycloak.user.disable", {"id": _ADMIN_UUID})
        guest = await _call("keycloak.user.disable", {"id": _SECOND_ADMIN_UUID})
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    # The member of that child group is no platform admin, so a write on them waits.
    assert guest["status"] == "awaiting_approval", guest
    assert written == []


async def test_a_child_group_without_its_own_value_takes_the_value_of_its_parent(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_CHILD_GROUP, "inherits", None, parent=_PLATFORM_GROUP)
        api.add(_user(_SECOND_ADMIN_UUID, "child-member"), _CHILD_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
    assert call["status"] == "awaiting_approval", call


async def test_the_platform_admin_group_without_the_attribute_gives_no_rights(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_NAME_ONLY_GROUP, "MEHO-Platform-Admins", None)
        api.groups[_NAME_ONLY_GROUP]["path"] = "/MEHO-Platform-Admins"
        api.add(_user(_SECOND_ADMIN_UUID, "name-only-member"), _NAME_ONLY_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        guest = await _call("keycloak.user.disable", {"id": _SECOND_ADMIN_UUID})
    _refused_before_approval(call, "the last active platform admin")
    assert guest["status"] == "awaiting_approval", guest


async def test_a_person_whose_groups_give_different_values_is_not_counted(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_CHILD_GROUP, "no-rights", "false")
        api.add(_user(_SECOND_ADMIN_UUID, "mixed-member"), _PLATFORM_GROUP, _CHILD_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
    _refused_before_approval(call, "the last active platform admin")


async def test_a_platform_admin_by_their_own_value_is_not_counted_as_another(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        own = {**_user(_SECOND_ADMIN_UUID, "own-value-admin")}
        own["attributes"] = {"platform_admin": ["true"]}
        api.add(own, _TEAM_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        # The user with the own value is a platform admin; the other one stays.
        theirs = await _call("keycloak.user.disable", {"id": _SECOND_ADMIN_UUID})
    _refused_before_approval(call, "the last active platform admin")
    assert theirs["status"] == "awaiting_approval", theirs


async def test_a_group_value_in_another_letter_case_makes_no_one_another_platform_admin(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_OTHER_ADMIN_GROUP, "other-case", None)
        api.groups[_OTHER_ADMIN_GROUP]["attributes"] = {"Platform_Admin": ["true"]}
        api.add(_user(_SECOND_ADMIN_UUID, "other-case-member"), _OTHER_ADMIN_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        result = await _run("keycloak.user.disable", {"id": _ADMIN_UUID})
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    assert written == []


@pytest.mark.parametrize(
    "value", [" true", "true ", " TRUE "], ids=["space-before", "space-after", "both"]
)
async def test_a_group_value_with_white_space_makes_no_one_another_platform_admin(
    kc: KeycloakConnector,  # noqa: F811
    value: str,
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_OTHER_ADMIN_GROUP, "white-space", None)
        api.groups[_OTHER_ADMIN_GROUP]["attributes"] = {"platform_admin": [value]}
        api.add(_user(_SECOND_ADMIN_UUID, "white-space-member"), _OTHER_ADMIN_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        result = await _run("keycloak.user.disable", {"id": _ADMIN_UUID})
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    assert written == []


async def test_a_group_value_under_the_claim_setting_makes_no_one_another_platform_admin(
    kc: KeycloakConnector,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_PLATFORM_ADMIN_CLAIM_NAME", "made_up_admin_flag")
    get_settings.cache_clear()
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_OTHER_ADMIN_GROUP, "claim-name", None)
        api.groups[_OTHER_ADMIN_GROUP]["attributes"] = {"made_up_admin_flag": ["true"]}
        api.add(_user(_SECOND_ADMIN_UUID, "claim-name-member"), _OTHER_ADMIN_GROUP)
        call = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        result = await _run("keycloak.user.disable", {"id": _ADMIN_UUID})
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    assert written == []


async def test_the_user_a_write_changes_is_a_platform_admin_by_any_letter_case(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.users.pop(_ADMIN_UUID)
        api.members[_PLATFORM_GROUP] = []
        api.group(_OTHER_ADMIN_GROUP, "other-case", None)
        api.groups[_OTHER_ADMIN_GROUP]["attributes"] = {"PLATFORM_ADMIN": ["true"]}
        api.add(_user(_SECOND_ADMIN_UUID, "other-case-member"), _OTHER_ADMIN_GROUP)
        call = await _call("keycloak.user.disable", {"id": _SECOND_ADMIN_UUID})
    _refused_before_approval(call, "the last active platform admin")


async def test_the_user_a_write_changes_is_a_platform_admin_by_a_value_with_white_space(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.users.pop(_ADMIN_UUID)
        api.members[_PLATFORM_GROUP] = []
        api.group(_OTHER_ADMIN_GROUP, "white-space", None)
        api.groups[_OTHER_ADMIN_GROUP]["attributes"] = {"platform_admin": [" true "]}
        api.add(_user(_SECOND_ADMIN_UUID, "white-space-member"), _OTHER_ADMIN_GROUP)
        call = await _call("keycloak.user.disable", {"id": _SECOND_ADMIN_UUID})
    _refused_before_approval(call, "the last active platform admin")


# ---------------------------------------------------------------------------
# Group writes
# ---------------------------------------------------------------------------

_GROUP_WRITES: list[tuple[str, dict[str, Any]]] = [
    ("keycloak.group.delete", {"id": _PLATFORM_GROUP}),
    (
        "keycloak.group.update_attributes",
        {"id": _PLATFORM_GROUP, "attributes": {"platform_admin": ["false"]}},
    ),
    (
        "keycloak.group.update_attributes",
        {"id": _PLATFORM_GROUP, "attributes": {"note": ["made-up"]}, "replace": True},
    ),
]
_GROUP_IDS = ["delete", "update-value-false", "update-replace-without-value"]


@pytest.mark.parametrize(("op_id", "params"), _GROUP_WRITES, ids=_GROUP_IDS)
async def test_a_group_write_that_takes_the_last_platform_admin_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        call = await _call(op_id, params)
        result = await _run(op_id, params)
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    assert await _stored_requests() == []
    assert written == []


@pytest.mark.parametrize(("op_id", "params"), _GROUP_WRITES, ids=_GROUP_IDS)
async def test_a_group_write_waits_when_another_platform_admin_keeps_the_rights(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.group(_OTHER_ADMIN_GROUP, "other-admins", "true")
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _OTHER_ADMIN_GROUP)
        call = await _call(op_id, params)
    assert call["status"] == "awaiting_approval", call


@pytest.mark.parametrize(("op_id", "params"), _GROUP_WRITES, ids=_GROUP_IDS)
async def test_a_group_write_is_refused_when_a_keycloak_read_fails(
    kc: KeycloakConnector,  # noqa: F811
    op_id: str,
    params: dict[str, Any],
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.members_fail = True
        call = await _call(op_id, params)
        result = await _run(op_id, params)
        written = _writes(mock)
    _refused_before_approval(call, "a Keycloak read failed")
    _refused_on_run(result, "meho_access_not_checked")
    assert written == []


async def test_a_group_write_that_keeps_the_value_is_not_counted(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        _realm(mock)
        call = await _call(
            "keycloak.group.update_attributes",
            {"id": _PLATFORM_GROUP, "attributes": {"note": ["made-up"]}},
        )
        deleted = await _call("keycloak.group.delete", {"id": _TEAM_GROUP})
    assert call["status"] == "awaiting_approval", call
    assert deleted["status"] == "awaiting_approval", deleted


@pytest.mark.parametrize("in_child", [False, True], ids=["group", "child-group"])
async def test_a_new_value_false_on_a_group_of_the_last_platform_admin_is_refused(
    kc: KeycloakConnector,  # noqa: F811
    in_child: bool,
) -> None:
    params = {"id": _TEAM_GROUP, "attributes": {"platform_admin": ["false"]}}
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        if in_child:
            api.group(_CHILD_GROUP, "team-child", None, parent=_TEAM_GROUP)
        api.add(api.users[_ADMIN_UUID], _CHILD_GROUP if in_child else _TEAM_GROUP)
        call = await _call("keycloak.group.update_attributes", params)
        result = await _run("keycloak.group.update_attributes", params)
        written = _writes(mock)
    _refused_before_approval(call, "the last active platform admin")
    _refused_on_run(result, "last_platform_admin")
    assert await _stored_requests() == []
    assert written == []


async def test_a_new_value_false_waits_when_another_platform_admin_keeps_the_rights(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(api.users[_ADMIN_UUID], _TEAM_GROUP)
        api.group(_OTHER_ADMIN_GROUP, "other-admins", "true")
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), _OTHER_ADMIN_GROUP)
        call = await _call(
            "keycloak.group.update_attributes",
            {"id": _TEAM_GROUP, "attributes": {"platform_admin": ["false"]}},
        )
    assert call["status"] == "awaiting_approval", call


async def test_deleting_a_parent_group_takes_the_rights_of_its_child_groups(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.members[_PLATFORM_GROUP] = []
        api.group(_CHILD_GROUP, "admins", "true", parent=_TEAM_GROUP)
        api.add(api.users[_ADMIN_UUID], _CHILD_GROUP)
        call = await _call("keycloak.group.delete", {"id": _TEAM_GROUP})
    _refused_before_approval(call, "the last active platform admin")


# ---------------------------------------------------------------------------
# Large realms
# ---------------------------------------------------------------------------


def _many_groups(api: Any, count: int) -> list[str]:
    ids = [f"bbbbbbbb-0000-0000-0000-{n:012d}" for n in range(count)]
    for n, group_id in enumerate(ids):
        api.group(group_id, f"team-{n:04d}", None)
    return ids


async def test_a_realm_with_more_than_1000_groups_is_counted_page_by_page(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        ids = _many_groups(api, 1200)
        # The only other platform admin is in a group on the last page.
        api.groups[ids[-1]]["attributes"] = {"platform_admin": ["true"]}
        api.add(_user(_SECOND_ADMIN_UUID, "second-platform-admin"), ids[-1])
        waits = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
        api.members[ids[-1]] = []
        refused = await _call("keycloak.user.disable", {"id": _ADMIN_UUID})
    assert waits["status"] == "awaiting_approval", waits
    _refused_before_approval(refused, "the last active platform admin")


async def test_a_person_who_is_no_platform_admin_needs_no_read_of_all_groups(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        _many_groups(api, 1200)
        api.add(_user(_GUEST_UUID, "event-guest"), _TEAM_GROUP)
        call = await _call("keycloak.user.disable", {"id": _GUEST_UUID})
        tree_reads = [
            c for c in mock.calls if c.request.url.path == f"/admin/realms/{_LOGIN_REALM}/groups"
        ]
    assert call["status"] == "awaiting_approval", call
    assert tree_reads == []


# ---------------------------------------------------------------------------
# A standing grant on group.member.remove
# ---------------------------------------------------------------------------


async def _grant(op_id: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                principal_sub="svc:access-bot",
                op_id=op_id,
                connector_id=_CONNECTOR_ID,
                target_id=(await _target(_LOGIN_REALM_TARGET)).id,
                reason="unattended clean-up",
                created_by_sub="kc-access-admin",
            )
        )
        await session.commit()


async def test_a_standing_grant_on_member_remove_does_not_remove_the_last_platform_admin(
    kc: KeycloakConnector,  # noqa: F811
) -> None:
    await _grant("keycloak.group.member.remove")
    with respx.mock(base_url=_KC_BASE_URL, assert_all_called=False) as mock:
        api = _realm(mock)
        api.add(_user(_BOT_UUID, f"service-account-{_BOT_CLIENT}"), _TEAM_GROUP)
        call = await _call(
            "keycloak.group.member.remove",
            {"user_id": _ADMIN_UUID, "group_id": _PLATFORM_GROUP},
            _bot(),
        )
        written = _writes(mock)
    assert call["status"] != "ok", call
    assert "the last active platform admin" in str(call), call
    assert written == []
