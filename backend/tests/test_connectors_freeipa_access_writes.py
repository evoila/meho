# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the eleven FreeIPA sudo, access rule (HBAC) and password policy writes.

Every test goes through ``dispatch()``. What is covered:

* each write waits until a second person approves;
* the approval preview of ``freeipa.hbacrule.disable`` names the rule and,
  for ``allow_all``, lists the other enabled access rules; the preview of
  ``freeipa.pwpolicy.set`` says when the change is for the global policy;
* after approval, each write sends one IPA command with the arguments and
  options it should, and returns the documented shape;
* a member FreeIPA did not add fails the call and is named; a sudo rule
  member that FreeIPA keeps as an external name is returned in ``external``;
* invalid parameters are refused before any call.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.freeipa import FREEIPA_IMPL_ID, FREEIPA_PRODUCT, FreeipaConnector
from meho_backplane.connectors.freeipa.access_writes import (
    PWPOLICY_OPTIONS,
    hbacrule_disable_preview,
)
from meho_backplane.connectors.freeipa.ops_access_writes import ACCESS_WRITE_OPS
from meho_backplane.connectors.freeipa.rpc import API_VERSION
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.settings import get_settings
from tests._freeipa_fixtures import (
    FREEIPA_VERSION,
    Target,
    error_answer,
    find_answer,
    make_operator,
    register,
    run,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=FREEIPA_PRODUCT,
        version=FREEIPA_VERSION,
        impl_id=FREEIPA_IMPL_ID,
        cls=FreeipaConnector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _options(**options: Any) -> dict[str, Any]:
    return {**options, "version": API_VERSION}


def _rendered(result: Any) -> str:
    return (result.error or "") + json.dumps(result.extras, default=str)


SUDO_RULE = "webadmins-systemctl"
HBAC_RULE = "webadmins-ssh"
SUDOCMD = {"sudocmd": ["/usr/bin/systemctl"], "description": ["Manage services"]}
SUDORULE = {"cn": [SUDO_RULE], "ipaenabledflag": [True]}
SUDORULE_MEMBERS = {
    **SUDORULE,
    "memberuser_group": ["webadmins"],
    "memberhost_hostgroup": ["webservers"],
    "memberallowcmd_sudocmd": ["/usr/bin/systemctl"],
}
HBACRULE = {"cn": [HBAC_RULE], "ipaenabledflag": [True], "accessruletype": ["allow"]}
HBACRULE_MEMBERS = {
    **HBACRULE,
    "memberuser_group": ["webadmins"],
    "memberhost_hostgroup": ["webservers"],
    "memberservice_hbacsvc": ["sshd"],
}
GROUP_POLICY = {"cn": ["admins"], "krbmaxpwdlife": ["90"], "krbpwdminlength": ["12"]}
GLOBAL_POLICY = {"cn": ["global_policy"], "krbpwdmaxfailure": ["5"]}


def created(entry: dict[str, Any], name: str) -> dict[str, Any]:
    return {"result": entry, "value": name, "summary": f'Added "{name}"'}


def members_added(
    attribute: str, completed: int, entry: dict[str, Any], **failed: list[Any]
) -> dict[str, Any]:
    """An add-member answer; *failed* holds FreeIPA's failed entries per member type."""
    return {"result": entry, "failed": {attribute: failed}, "completed": completed}


def policy_changed(entry: dict[str, Any], name: str) -> dict[str, Any]:
    return {"result": entry, "value": name, "summary": None}


#: The pwpolicy_mod option of each settings parameter, as named in API.txt of 4.13.4.
_ALL_SETTINGS = {
    "max_life_days": 90,
    "min_life_hours": 1,
    "history_size": 6,
    "min_classes": 3,
    "min_length": 12,
    "max_failures": 5,
    "failure_reset_seconds": 60,
    "lockout_seconds": 600,
    "priority": 10,
}
_ALL_OPTIONS = {
    "krbmaxpwdlife": 90,
    "krbminpwdlife": 1,
    "krbpwdhistorylength": 6,
    "krbpwdmindiffchars": 3,
    "krbpwdminlength": 12,
    "krbpwdmaxfailure": 5,
    "krbpwdfailurecountinterval": 60,
    "krbpwdlockoutduration": 600,
    "cospriority": 10,
}

#: (op id, params, command, arguments, options, answer, result) for every write.
_WRITES: list[tuple[str, dict[str, Any], str, list[Any], dict[str, Any], Any, Any]] = [
    (
        "freeipa.sudocmd.create",
        {"command": "/usr/bin/systemctl", "description": "Manage services"},
        "sudocmd_add",
        ["/usr/bin/systemctl"],
        _options(description="Manage services"),
        created(SUDOCMD, "/usr/bin/systemctl"),
        {"entry": SUDOCMD},
    ),
    (
        "freeipa.sudorule.create",
        {"name": SUDO_RULE},
        "sudorule_add",
        [SUDO_RULE],
        _options(),
        created(SUDORULE, SUDO_RULE),
        {"entry": SUDORULE},
    ),
    (
        "freeipa.sudorule.user.add",
        {"name": SUDO_RULE, "users": ["jdoe"], "groups": ["webadmins"]},
        "sudorule_add_user",
        [SUDO_RULE],
        _options(user=["jdoe"], group=["webadmins"]),
        members_added("memberuser", 2, SUDORULE_MEMBERS, user=[], group=[]),
        {"entry": SUDORULE_MEMBERS, "completed": 2, "external": []},
    ),
    (
        "freeipa.sudorule.host.add",
        {"name": SUDO_RULE, "hostgroups": ["webservers"]},
        "sudorule_add_host",
        [SUDO_RULE],
        _options(hostgroup=["webservers"]),
        members_added("memberhost", 1, SUDORULE_MEMBERS, host=[], hostgroup=[]),
        {"entry": SUDORULE_MEMBERS, "completed": 1, "external": []},
    ),
    (
        "freeipa.sudorule.command.add",
        {"name": SUDO_RULE, "commands": ["/usr/bin/systemctl"]},
        "sudorule_add_allow_command",
        [SUDO_RULE],
        _options(sudocmd=["/usr/bin/systemctl"]),
        members_added("memberallowcmd", 1, SUDORULE_MEMBERS, sudocmd=[], sudocmdgroup=[]),
        {"entry": SUDORULE_MEMBERS, "completed": 1},
    ),
    (
        "freeipa.hbacrule.create",
        {"name": HBAC_RULE, "description": "Web admins log in to web servers"},
        "hbacrule_add",
        [HBAC_RULE],
        _options(description="Web admins log in to web servers"),
        created(HBACRULE, HBAC_RULE),
        {"entry": HBACRULE},
    ),
    (
        "freeipa.hbacrule.user.add",
        {"name": HBAC_RULE, "groups": ["webadmins"]},
        "hbacrule_add_user",
        [HBAC_RULE],
        _options(group=["webadmins"]),
        members_added("memberuser", 1, HBACRULE_MEMBERS, user=[], group=[]),
        {"entry": HBACRULE_MEMBERS, "completed": 1},
    ),
    (
        "freeipa.hbacrule.host.add",
        {"name": HBAC_RULE, "hosts": ["web01.example.test"], "hostgroups": ["webservers"]},
        "hbacrule_add_host",
        [HBAC_RULE],
        _options(host=["web01.example.test"], hostgroup=["webservers"]),
        members_added("memberhost", 2, HBACRULE_MEMBERS, host=[], hostgroup=[]),
        {"entry": HBACRULE_MEMBERS, "completed": 2},
    ),
    (
        "freeipa.hbacrule.service.add",
        {"name": HBAC_RULE, "services": ["sshd"], "service_groups": ["Sudo"]},
        "hbacrule_add_service",
        [HBAC_RULE],
        _options(hbacsvc=["sshd"], hbacsvcgroup=["Sudo"]),
        members_added("memberservice", 2, HBACRULE_MEMBERS, hbacsvc=[], hbacsvcgroup=[]),
        {"entry": HBACRULE_MEMBERS, "completed": 2},
    ),
    (
        "freeipa.hbacrule.disable",
        {"name": HBAC_RULE},
        "hbacrule_disable",
        [HBAC_RULE],
        _options(),
        {"result": True, "value": HBAC_RULE, "summary": f'Disabled HBAC rule "{HBAC_RULE}"'},
        {"disabled": HBAC_RULE},
    ),
    (
        "freeipa.pwpolicy.set",
        {"group": "admins", "max_life_days": 90, "min_length": 12},
        "pwpolicy_mod",
        ["admins"],
        _options(krbmaxpwdlife=90, krbpwdminlength=12),
        policy_changed(GROUP_POLICY, "admins"),
        {"entry": GROUP_POLICY},
    ),
    (
        "freeipa.pwpolicy.set",
        {"group": "admins", **_ALL_SETTINGS},
        "pwpolicy_mod",
        ["admins"],
        _options(**_ALL_OPTIONS),
        policy_changed(GROUP_POLICY, "admins"),
        {"entry": GROUP_POLICY},
    ),
    (
        "freeipa.pwpolicy.set",
        {"max_failures": 5},
        "pwpolicy_mod",
        [],
        _options(krbpwdmaxfailure=5),
        policy_changed(GLOBAL_POLICY, "global_policy"),
        {"entry": GLOBAL_POLICY},
    ),
]


def test_every_write_is_covered_here() -> None:
    assert {row[0] for row in _WRITES} == {op.op_id for op in ACCESS_WRITE_OPS}


def test_every_write_is_a_change_that_waits_for_approval() -> None:
    assert len(ACCESS_WRITE_OPS) == 11
    for op in ACCESS_WRITE_OPS:
        assert op.safety_level == "caution", op.op_id
        assert op.requires_approval is True, op.op_id
        assert "write" in op.tags and "read-only" not in op.tags, op.op_id


def test_pwpolicy_parameters_are_the_sent_options() -> None:
    (op,) = [op for op in ACCESS_WRITE_OPS if op.op_id == "freeipa.pwpolicy.set"]
    assert set(op.parameter_schema["properties"]) == {"group", *PWPOLICY_OPTIONS}
    assert set(_ALL_SETTINGS) == set(PWPOLICY_OPTIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command", "args", "options", "answer", "expected"), _WRITES
)
async def test_approved_write_sends_its_command(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
    args: list[Any],
    options: dict[str, Any],
    answer: Any,
    expected: Any,
) -> None:
    await register()
    result, calls, _ = await run(op_id, params, {command: answer}, approved=True)

    assert result.status == "ok", result.error
    assert [call["body"] for call in calls] == [
        {"method": command, "params": [args, options], "id": 0}
    ]
    assert result.result == expected


# ---------------------------------------------------------------------------
# A change FreeIPA did not make is never "ok"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command", "answer", "named"),
    [
        (
            "freeipa.sudorule.user.add",
            {"name": SUDO_RULE, "users": ["jdoe"], "groups": ["webadmins"]},
            "sudorule_add_user",
            members_added(
                "memberuser",
                1,
                SUDORULE_MEMBERS,
                user=[["jdoe", "This entry is already a member"]],
                group=[],
            ),
            "user jdoe: This entry is already a member",
        ),
        (
            "freeipa.sudorule.command.add",
            {"name": SUDO_RULE, "commands": ["/usr/bin/systemctl", "/usr/bin/vi"]},
            "sudorule_add_allow_command",
            members_added(
                "memberallowcmd",
                1,
                SUDORULE_MEMBERS,
                sudocmd=[["/usr/bin/vi", "no such entry"]],
                sudocmdgroup=[],
            ),
            "sudocmd /usr/bin/vi: no such entry",
        ),
        (
            "freeipa.hbacrule.user.add",
            {"name": HBAC_RULE, "users": ["jdoe", "jdoe2"]},
            "hbacrule_add_user",
            members_added(
                "memberuser", 1, HBACRULE_MEMBERS, user=[["jdoe2", "no such entry"]], group=[]
            ),
            "user jdoe2: no such entry",
        ),
        (
            "freeipa.hbacrule.service.add",
            {"name": HBAC_RULE, "services": ["sshd", "telnet"]},
            "hbacrule_add_service",
            members_added(
                "memberservice",
                1,
                HBACRULE_MEMBERS,
                hbacsvc=[["telnet", "no such entry"]],
                hbacsvcgroup=[],
            ),
            "hbacsvc telnet: no such entry",
        ),
    ],
)
async def test_member_add_fails_and_names_each_member_freeipa_did_not_add(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
    answer: dict[str, Any],
    named: str,
) -> None:
    await register()
    result, _, _ = await run(op_id, params, {command: answer}, approved=True)

    assert result.status == "error"
    rendered = _rendered(result)
    assert command in rendered
    assert "1 of 2" in rendered
    assert named in rendered


@pytest.mark.asyncio
async def test_member_add_with_a_low_completed_count_fails(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    answer = members_added("memberhost", 0, HBACRULE_MEMBERS, host=[], hostgroup=[])
    params = {"name": HBAC_RULE, "hostgroups": ["webservers"]}
    result, _, _ = await run(
        "freeipa.hbacrule.host.add", params, {"hbacrule_add_host": answer}, approved=True
    )
    assert result.status == "error"
    assert "0 of 1" in _rendered(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command", "answer", "external"),
    [
        (
            "freeipa.sudorule.user.add",
            {"name": SUDO_RULE, "users": ["jdoe", "LocalAdmin"]},
            "sudorule_add_user",
            members_added(
                "memberuser",
                2,
                {**SUDORULE, "memberuser_user": ["jdoe"], "externaluser": ["localadmin"]},
                user=[],
                group=[],
            ),
            ["LocalAdmin"],
        ),
        (
            "freeipa.sudorule.user.add",
            {"name": SUDO_RULE, "users": ["jdoe"], "groups": ["localadmins"]},
            "sudorule_add_user",
            members_added(
                "memberuser",
                2,
                {**SUDORULE, "memberuser_user": ["jdoe"], "externaluser": ["%localadmins"]},
                user=[],
                group=[],
            ),
            ["localadmins"],
        ),
        (
            "freeipa.sudorule.user.add",
            {"name": SUDO_RULE, "groups": ["webops", "dbops"]},
            "sudorule_add_user",
            members_added(
                "memberuser",
                2,
                {**SUDORULE, "memberuser_group": ["dbops"], "externaluser": ["webops"]},
                user=[],
                group=[],
            ),
            ["webops"],
        ),
        (
            "freeipa.sudorule.user.add",
            {"name": SUDO_RULE, "users": ["bob", "jdoe"]},
            "sudorule_add_user",
            members_added(
                "memberuser",
                2,
                {**SUDORULE, "memberuser_user": ["bob", "jdoe"], "externaluser": ["%bob"]},
                user=[],
                group=[],
            ),
            [],
        ),
        (
            "freeipa.sudorule.host.add",
            {"name": SUDO_RULE, "hosts": ["web01.example.test", "legacy01.example.test"]},
            "sudorule_add_host",
            members_added(
                "memberhost",
                2,
                {
                    **SUDORULE,
                    "memberhost_host": ["web01.example.test"],
                    "externalhost": ["legacy01.example.test"],
                },
                host=[],
                hostgroup=[],
            ),
            ["legacy01.example.test"],
        ),
    ],
)
async def test_sudo_rule_returns_the_names_freeipa_keeps_as_external(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
    answer: dict[str, Any],
    external: list[str],
) -> None:
    await register()
    result, _, _ = await run(op_id, params, {command: answer}, approved=True)
    assert result.status == "ok", result.error
    assert result.result["completed"] == 2
    assert result.result["external"] == external


@pytest.mark.asyncio
async def test_command_error_of_a_policy_change_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    unchanged = error_answer("EmptyModlist", 4202, "no modifications to be performed")
    result, _, _ = await run(
        "freeipa.pwpolicy.set", {"min_length": 8}, {"pwpolicy_mod": unchanged}, approved=True
    )
    assert result.status == "error"
    assert "EmptyModlist" in _rendered(result)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def _proposed_effect(result: Any) -> dict[str, Any]:
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
    assert row is not None
    return row.proposed_effect


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (row[0], row[1])
        for row in _WRITES
        if row[0] not in {"freeipa.hbacrule.disable", "freeipa.pwpolicy.set"}
    ],
)
async def test_a_change_waits_for_approval_before_any_call(
    _stub_embedding: AsyncMock, session: AsyncSession, op_id: str, params: dict[str, Any]
) -> None:
    await register()
    result, calls, login = await run(op_id, params)

    assert result.status == "awaiting_approval", result.error
    assert calls == []
    assert login.call_count == 0
    effect = await _proposed_effect(result)
    assert effect["safety_level"] == "caution"
    assert effect["params_echo"] == params


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["allow_all", "Allow_All"])
async def test_disabling_allow_all_waits_and_lists_the_other_enabled_rules(
    _stub_embedding: AsyncMock, session: AsyncSession, name: str
) -> None:
    await register()
    enabled = find_answer([{"cn": ["allow_all"]}, {"cn": [HBAC_RULE]}, {"cn": ["ops-sudo"]}])
    result, calls, _ = await run(
        "freeipa.hbacrule.disable", {"name": name}, {"hbacrule_find": enabled}
    )

    assert result.status == "awaiting_approval", result.error
    assert [call["body"] for call in calls] == [
        {
            "method": "hbacrule_find",
            "params": [[], _options(ipaenabledflag=True, pkey_only=True, sizelimit=2000)],
            "id": 0,
        }
    ]
    effect = await _proposed_effect(result)
    assert effect["safety_level"] == "caution"
    preview = effect["preview"]
    assert preview["rule"] == name
    assert preview["other_enabled_rules"] == [HBAC_RULE, "ops-sudo"]
    assert preview["truncated"] is False
    assert "allow_all is FreeIPA's default access rule" in preview["warning"]
    assert "only the other enabled access rules let users in" in preview["warning"]
    assert "no IPA user can log in" not in preview["warning"]


@pytest.mark.asyncio
async def test_disabling_allow_all_as_the_only_enabled_rule_says_nobody_can_log_in(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    only = find_answer([{"cn": ["allow_all"]}])
    result, _, _ = await run(
        "freeipa.hbacrule.disable", {"name": "allow_all"}, {"hbacrule_find": only}
    )

    assert result.status == "awaiting_approval", result.error
    preview = (await _proposed_effect(result))["preview"]
    assert preview["other_enabled_rules"] == []
    assert "no IPA user can log in to any host that uses IPA" in preview["warning"]


@pytest.mark.asyncio
async def test_disabling_another_rule_names_it_without_a_call(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, calls, login = await run("freeipa.hbacrule.disable", {"name": HBAC_RULE})

    assert result.status == "awaiting_approval", result.error
    assert calls == []
    assert login.call_count == 0
    assert (await _proposed_effect(result))["preview"] == {"rule": HBAC_RULE}


@pytest.mark.asyncio
async def test_disabling_allow_all_still_waits_when_the_preview_read_fails(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    refused = error_answer("ACIError", 2100, "Insufficient access")
    result, _, _ = await run(
        "freeipa.hbacrule.disable", {"name": "allow_all"}, {"hbacrule_find": refused}
    )

    assert result.status == "awaiting_approval", result.error
    effect = await _proposed_effect(result)
    assert effect["preview_unavailable"] is True
    assert "Insufficient access" in effect["preview_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "expected"), [("allow_all", None), (HBAC_RULE, {"rule": HBAC_RULE})]
)
async def test_hbacrule_disable_preview_without_a_live_connector(
    name: str, expected: dict[str, Any] | None
) -> None:
    ctx = PreviewContext(
        descriptor=EndpointDescriptor(op_id="freeipa.hbacrule.disable"),
        connector_instance=None,
        operator=make_operator(),
        target=Target(),
        params={"name": name},
    )
    assert await hbacrule_disable_preview(ctx) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "preview"),
    [
        (
            {"max_life_days": 90},
            {
                "policy": "global_policy",
                "warning": (
                    "This changes the global password policy. It applies to every user whose "
                    "groups have no password policy of their own."
                ),
                "changes": {"max_life_days": 90},
            },
        ),
        (
            {"group": "global_policy", "min_length": 12},
            {
                "policy": "global_policy",
                "warning": (
                    "This changes the global password policy. It applies to every user whose "
                    "groups have no password policy of their own."
                ),
                "changes": {"min_length": 12},
            },
        ),
        (
            {"group": "Global_Policy", "history_size": 6},
            {
                "policy": "global_policy",
                "warning": (
                    "This changes the global password policy. It applies to every user whose "
                    "groups have no password policy of their own."
                ),
                "changes": {"history_size": 6},
            },
        ),
        (
            {"group": "admins", "min_length": 12, "priority": 5},
            {"policy": "admins", "changes": {"min_length": 12, "priority": 5}},
        ),
    ],
)
async def test_policy_change_waits_and_says_when_it_is_the_global_policy(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    params: dict[str, Any],
    preview: dict[str, Any],
) -> None:
    await register()
    result, calls, login = await run("freeipa.pwpolicy.set", params)

    assert result.status == "awaiting_approval", result.error
    assert calls == []
    assert login.call_count == 0
    effect = await _proposed_effect(result)
    assert effect["safety_level"] == "caution"
    assert effect["preview"] == preview


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("freeipa.sudocmd.create", {}),
        ("freeipa.sudorule.create", {"name": SUDO_RULE, "usercategory": "all"}),
        ("freeipa.sudorule.user.add", {"name": SUDO_RULE}),
        ("freeipa.sudorule.user.add", {"name": SUDO_RULE, "users": []}),
        ("freeipa.sudorule.command.add", {"name": SUDO_RULE}),
        ("freeipa.hbacrule.service.add", {"name": HBAC_RULE, "services": ["sshd", "sshd"]}),
        ("freeipa.hbacrule.disable", {}),
        ("freeipa.pwpolicy.set", {}),
        ("freeipa.pwpolicy.set", {"group": "admins"}),
        ("freeipa.pwpolicy.set", {"priority": 1}),
        ("freeipa.pwpolicy.set", {"min_classes": 6}),
        ("freeipa.pwpolicy.set", {"max_life_days": -1}),
        ("freeipa.pwpolicy.set", {"max_life_days": 90, "krbmaxpwdlife": 90}),
    ],
)
async def test_invalid_params_are_refused_before_any_call(
    _stub_embedding: AsyncMock, session: AsyncSession, op_id: str, params: dict[str, Any]
) -> None:
    await register()
    result, calls, login = await run(op_id, params)
    assert result.status == "error"
    assert calls == []
    assert login.call_count == 0
