# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the eight FreeIPA user, group and role writes.

Every test goes through ``dispatch()``. What is covered:

* each write waits until a second person approves, and the risk level is
  ``caution`` for a change and ``dangerous`` for a delete and for role members;
* the approval previews of the two deletes and of the role member add show
  what the change affects;
* after approval, each write sends one IPA command with the arguments and
  options it should, and returns the documented shape;
* no write takes or returns a password;
* a member FreeIPA did not add fails the call and is named;
* an expiry date that is not in the future is refused before any call;
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
from meho_backplane.connectors.freeipa.identity_writes import role_delete_preview
from meho_backplane.connectors.freeipa.ops_identity_writes import IDENTITY_WRITE_OPS
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


USER = {"uid": ["jdoe"], "givenname": ["Jane"], "sn": ["Doe"], "memberof_group": ["ipausers"]}
GROUP = {"cn": ["webadmins"], "gidnumber": ["1200000001"]}
ROLE = {
    "cn": ["Enrollment Administrator"],
    "memberof_privilege": ["Host Enrollment"],
    "member_user": ["svc-join"],
}
ROLE_NAME = "Enrollment Administrator"
FAR = "2099-01-01T00:00:00Z"
FAR_IPA = {"__datetime__": "20990101000000Z"}
MADE_UP_PASSWORD = "Made-Up-Password-1"


def created(entry: dict[str, Any], name: str) -> dict[str, Any]:
    return {"result": entry, "value": name, "summary": f'Added "{name}"'}


def members_added(
    attribute: str, completed: int, entry: dict[str, Any], **failed: list[Any]
) -> dict[str, Any]:
    """An add-member answer; *failed* holds FreeIPA's failed entries per member type."""
    return {"result": entry, "failed": {attribute: failed}, "completed": completed}


def deleted(name: str, **extra: Any) -> dict[str, Any]:
    return {"result": {"failed": []}, "value": [name], "summary": f'Deleted "{name}"', **extra}


#: (op id, params, command, arguments, options, answer, result) for every write.
_WRITES: list[tuple[str, dict[str, Any], str, list[Any], dict[str, Any], Any, Any]] = [
    (
        "freeipa.user.create",
        {"login": "jdoe", "first_name": "Jane", "last_name": "Doe"},
        "user_add",
        ["jdoe"],
        _options(givenname="Jane", sn="Doe"),
        created(USER, "jdoe"),
        {"entry": USER},
    ),
    (
        "freeipa.user.create",
        {"login": "jdoe", "first_name": "Jane", "last_name": "Doe", "mail": "jdoe@example.test"},
        "user_add",
        ["jdoe"],
        _options(givenname="Jane", sn="Doe", mail=["jdoe@example.test"]),
        created(USER, "jdoe"),
        {"entry": USER},
    ),
    (
        "freeipa.user.password_expiry.set",
        {"login": "svc-backup", "expires": FAR},
        "user_mod",
        ["svc-backup"],
        _options(krbpasswordexpiration=FAR_IPA),
        {"result": USER, "value": "svc-backup", "summary": 'Modified user "svc-backup"'},
        {"entry": USER},
    ),
    (
        "freeipa.user.disable",
        {"login": "jdoe"},
        "user_disable",
        ["jdoe"],
        _options(),
        {"result": True, "value": "jdoe", "summary": 'Disabled user account "jdoe"'},
        {"disabled": "jdoe"},
    ),
    (
        "freeipa.group.create",
        {"name": "webadmins", "description": "Web admins"},
        "group_add",
        ["webadmins"],
        _options(description="Web admins"),
        created(GROUP, "webadmins"),
        {"entry": GROUP},
    ),
    (
        "freeipa.group.create",
        {"name": "webadmins"},
        "group_add",
        ["webadmins"],
        _options(),
        created(GROUP, "webadmins"),
        {"entry": GROUP},
    ),
    (
        "freeipa.group.member.add",
        {"name": "webadmins", "users": ["jdoe"], "groups": ["ops"]},
        "group_add_member",
        ["webadmins"],
        _options(user=["jdoe"], group=["ops"]),
        members_added("member", 2, GROUP, user=[], group=[]),
        {"entry": GROUP, "completed": 2},
    ),
    (
        "freeipa.role.member.add",
        {
            "name": ROLE_NAME,
            "users": ["svc-join"],
            "groups": ["ops"],
            "hosts": ["web01.example.test"],
            "hostgroups": ["webservers"],
        },
        "role_add_member",
        [ROLE_NAME],
        _options(
            user=["svc-join"], group=["ops"], host=["web01.example.test"], hostgroup=["webservers"]
        ),
        members_added("member", 4, ROLE, user=[], group=[], host=[], hostgroup=[]),
        {"entry": ROLE, "completed": 4},
    ),
    (
        "freeipa.user.delete",
        {"login": "jdoe"},
        "user_del",
        ["jdoe"],
        _options(),
        deleted("jdoe"),
        {"deleted": ["jdoe"], "warnings": []},
    ),
    (
        "freeipa.role.delete",
        {"name": ROLE_NAME},
        "role_del",
        [ROLE_NAME],
        _options(),
        deleted(ROLE_NAME, messages=[{"message": "Made-up warning"}]),
        {"deleted": [ROLE_NAME], "warnings": ["Made-up warning"]},
    ),
]

#: The writes that have a preview of their own; the others show the echo of the parameters.
_PREVIEWED = {"freeipa.user.delete", "freeipa.role.delete", "freeipa.role.member.add"}


def test_every_write_is_covered_here() -> None:
    assert {row[0] for row in _WRITES} == {op.op_id for op in IDENTITY_WRITE_OPS}


def test_every_write_waits_for_approval_and_has_the_right_risk_level() -> None:
    assert len(IDENTITY_WRITE_OPS) == 8
    dangerous = {"freeipa.user.delete", "freeipa.role.delete", "freeipa.role.member.add"}
    for op in IDENTITY_WRITE_OPS:
        expected = "dangerous" if op.op_id in dangerous else "caution"
        assert op.safety_level == expected, op.op_id
        assert op.requires_approval is True, op.op_id
        assert "write" in op.tags and "read-only" not in op.tags, op.op_id
        assert "password" not in op.tags, op.op_id


def test_no_write_takes_a_password() -> None:
    forbidden = {"password", "userpassword", "random", "randompassword"}
    for op in IDENTITY_WRITE_OPS:
        assert not forbidden & set(op.parameter_schema["properties"]), op.op_id
        assert op.parameter_schema["additionalProperties"] is False, op.op_id


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


@pytest.mark.asyncio
async def test_user_create_sends_no_password_and_no_random_option(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    params = {"login": "jdoe", "first_name": "Jane", "last_name": "Doe"}
    result, calls, _ = await run(
        "freeipa.user.create", params, {"user_add": created(USER, "jdoe")}, approved=True
    )
    assert result.status == "ok", result.error
    sent = calls[0]["body"]["params"][1]
    assert not {"userpassword", "random"} & set(sent)


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
    [(row[0], row[1]) for row in _WRITES if row[0] not in _PREVIEWED],
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
async def test_deleting_a_user_waits_and_shows_the_groups_and_roles_the_user_leaves(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    shown = {
        "result": {
            "uid": ["jdoe"],
            "memberof_group": ["webadmins"],
            "memberofindirect_group": ["ops"],
            "memberof_role": [ROLE_NAME],
        },
        "value": "jdoe",
        "summary": None,
    }
    result, calls, _ = await run("freeipa.user.delete", {"login": "jdoe"}, {"user_show": shown})

    assert result.status == "awaiting_approval", result.error
    assert [call["body"]["method"] for call in calls] == ["user_show"]
    effect = await _proposed_effect(result)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"] == {
        "user": "jdoe",
        "groups": ["webadmins"],
        "indirect_groups": ["ops"],
        "roles": [ROLE_NAME],
    }


@pytest.mark.asyncio
async def test_deleting_a_role_waits_and_shows_its_members_and_privileges(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    shown = {"result": ROLE, "value": ROLE_NAME, "summary": None}
    result, calls, _ = await run("freeipa.role.delete", {"name": ROLE_NAME}, {"role_show": shown})

    assert result.status == "awaiting_approval", result.error
    assert [call["body"]["method"] for call in calls] == ["role_show"]
    effect = await _proposed_effect(result)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"] == {
        "role": ROLE_NAME,
        "privileges": ["Host Enrollment"],
        "members": {"users": ["svc-join"], "groups": [], "hosts": [], "hostgroups": []},
    }


@pytest.mark.asyncio
async def test_adding_role_members_waits_and_shows_the_privileges_they_get(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    shown = {"result": ROLE, "value": ROLE_NAME, "summary": None}
    params = {"name": ROLE_NAME, "users": ["jdoe"], "hostgroups": ["webservers"]}
    result, calls, _ = await run("freeipa.role.member.add", params, {"role_show": shown})

    assert result.status == "awaiting_approval", result.error
    assert [call["body"]["method"] for call in calls] == ["role_show"]
    effect = await _proposed_effect(result)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"] == {
        "role": ROLE_NAME,
        "privileges": ["Host Enrollment"],
        "new_members": {"users": ["jdoe"], "groups": [], "hosts": [], "hostgroups": ["webservers"]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command"),
    [
        ("freeipa.user.delete", {"login": "ghost"}, "user_show"),
        ("freeipa.role.delete", {"name": "No such role"}, "role_show"),
        ("freeipa.role.member.add", {"name": "No such role", "users": ["jdoe"]}, "role_show"),
    ],
)
async def test_a_failed_preview_read_still_waits_and_says_why(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
) -> None:
    await register()
    missing = error_answer("NotFound", 4001, "no such entry")
    result, _, _ = await run(op_id, params, {command: missing})

    assert result.status == "awaiting_approval", result.error
    effect = await _proposed_effect(result)
    assert effect["preview_unavailable"] is True
    assert "no such entry" in effect["preview_error"]


@pytest.mark.asyncio
async def test_a_preview_without_a_live_connector_declines() -> None:
    ctx = PreviewContext(
        descriptor=EndpointDescriptor(op_id="freeipa.role.delete"),
        connector_instance=None,
        operator=make_operator(),
        target=Target(),
        params={"name": ROLE_NAME},
    )
    assert await role_delete_preview(ctx) is None


# ---------------------------------------------------------------------------
# A change FreeIPA did not make is never "ok"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command", "answer", "named", "sent"),
    [
        (
            "freeipa.group.member.add",
            {"name": "webadmins", "users": ["jdoe", "ghost"]},
            "group_add_member",
            members_added("member", 1, GROUP, user=[["ghost", "no such entry"]], group=[]),
            "user ghost: no such entry",
            2,
        ),
        (
            "freeipa.group.member.add",
            {"name": "webadmins", "users": ["jdoe", "bob"]},
            "group_add_member",
            members_added(
                "member", 1, GROUP, user=[["bob", "This entry is already a member"]], group=[]
            ),
            "user bob: This entry is already a member",
            2,
        ),
        (
            "freeipa.role.member.add",
            {"name": ROLE_NAME, "users": ["jdoe"], "hostgroups": ["nohosts"]},
            "role_add_member",
            members_added(
                "member",
                1,
                ROLE,
                user=[],
                group=[],
                host=[],
                hostgroup=[["nohosts", "no such entry"]],
            ),
            "hostgroup nohosts: no such entry",
            2,
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
    sent: int,
) -> None:
    await register()
    result, _, _ = await run(op_id, params, {command: answer}, approved=True)

    assert result.status == "error"
    rendered = _rendered(result)
    assert command in rendered
    assert f"1 of {sent}" in rendered
    assert named in rendered


@pytest.mark.asyncio
async def test_member_add_with_a_low_completed_count_fails(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    answer = members_added("member", 0, GROUP, user=[], group=[])
    result, _, _ = await run(
        "freeipa.group.member.add",
        {"name": "webadmins", "users": ["jdoe"]},
        {"group_add_member": answer},
        approved=True,
    )
    assert result.status == "error"
    assert "0 of 1" in _rendered(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command"),
    [
        ("freeipa.user.delete", {"login": "jdoe"}, "user_del"),
        ("freeipa.role.delete", {"name": ROLE_NAME}, "role_del"),
    ],
)
async def test_a_delete_with_a_failed_name_fails(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
) -> None:
    await register()
    answer = {"result": {"failed": ["jdoe"]}, "value": [], "summary": "Deleted"}
    result, _, _ = await run(op_id, params, {command: answer}, approved=True)
    assert result.status == "error"
    assert "Not done" in _rendered(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "command"),
    [
        ("freeipa.user.create", {"login": "jdoe", "first_name": "J", "last_name": "D"}, "user_add"),
        ("freeipa.user.disable", {"login": "jdoe"}, "user_disable"),
        ("freeipa.user.delete", {"login": "ghost"}, "user_del"),
        ("freeipa.group.create", {"name": "webadmins"}, "group_add"),
        ("freeipa.role.delete", {"name": "No such role"}, "role_del"),
    ],
)
async def test_command_error_reaches_the_answer(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
) -> None:
    await register()
    refused = error_answer("NotFound", 4001, "no such entry")
    result, _, _ = await run(op_id, params, {command: refused}, approved=True)
    assert result.status == "error"
    assert "NotFound" in _rendered(result)


# ---------------------------------------------------------------------------
# The expiry date
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("expires", ["2001-01-01T00:00:00Z", "2099-02-30T00:00:00Z"])
async def test_an_expiry_date_that_is_past_or_not_a_date_is_refused_before_any_call(
    _stub_embedding: AsyncMock, session: AsyncSession, expires: str
) -> None:
    await register()
    result, calls, _ = await run(
        "freeipa.user.password_expiry.set",
        {"login": "svc-backup", "expires": expires},
        {"user_mod": {"result": USER, "value": "svc-backup", "summary": None}},
        approved=True,
    )
    assert result.status == "error"
    assert calls == []


# ---------------------------------------------------------------------------
# No password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        (
            "freeipa.user.create",
            {"login": "jdoe", "first_name": "J", "last_name": "D", "password": MADE_UP_PASSWORD},
        ),
        (
            "freeipa.user.create",
            {
                "login": "jdoe",
                "first_name": "J",
                "last_name": "D",
                "userpassword": MADE_UP_PASSWORD,
            },
        ),
        (
            "freeipa.user.create",
            {"login": "jdoe", "first_name": "J", "last_name": "D", "random": True},
        ),
        (
            "freeipa.user.password_expiry.set",
            {"login": "jdoe", "expires": FAR, "password": MADE_UP_PASSWORD},
        ),
    ],
)
async def test_a_password_parameter_is_refused_and_never_shown(
    _stub_embedding: AsyncMock, session: AsyncSession, op_id: str, params: dict[str, Any]
) -> None:
    await register()
    result, calls, login = await run(op_id, params)
    assert result.status == "error"
    assert calls == []
    assert login.call_count == 0
    assert MADE_UP_PASSWORD not in _rendered(result)


# ---------------------------------------------------------------------------
# Invalid parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("freeipa.user.create", {"login": "jdoe"}),
        ("freeipa.user.create", {"login": "", "first_name": "J", "last_name": "D"}),
        ("freeipa.user.password_expiry.set", {"login": "jdoe"}),
        ("freeipa.user.password_expiry.set", {"login": "jdoe", "expires": "next year"}),
        ("freeipa.user.password_expiry.set", {"login": "jdoe", "expires": "2099-01-01"}),
        ("freeipa.user.disable", {}),
        ("freeipa.group.create", {}),
        ("freeipa.group.member.add", {"name": "webadmins"}),
        ("freeipa.group.member.add", {"name": "webadmins", "users": []}),
        ("freeipa.group.member.add", {"name": "webadmins", "users": ["jdoe", "jdoe"]}),
        ("freeipa.role.member.add", {"name": ROLE_NAME}),
        ("freeipa.role.member.add", {"name": ROLE_NAME, "services": ["x"]}),
        ("freeipa.user.delete", {}),
        ("freeipa.user.delete", {"login": "jdoe", "preserve": True}),
        ("freeipa.role.delete", {}),
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
