# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the six FreeIPA host writes, through ``dispatch()``.

What is covered:

* each write waits until a second person approves; the approval preview of
  ``freeipa.host.delete`` shows the host and its host groups;
* after approval, each write sends one IPA command with the arguments and
  options it should, and returns the documented shape;
* a write whose answer lists ``failed`` entries, or whose ``completed`` count
  is lower than the values sent, fails and names what FreeIPA did not do;
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
from meho_backplane.connectors.freeipa.ops_host_writes import HOST_WRITE_OPS
from meho_backplane.connectors.freeipa.rpc import API_VERSION
from meho_backplane.connectors.freeipa.writes import host_delete_preview
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


HOSTGROUP = {"cn": ["webservers"], "description": ["Web servers"]}
MEMBERS = {"cn": ["webservers"], "member_host": ["web01.example.test", "web02.example.test"]}
RULE = {
    "cn": ["webservers"],
    "automembertargetgroup": ["cn=webservers,cn=hostgroups,cn=accounts,dc=example,dc=test"],
}
CONDITIONS = {**RULE, "automemberinclusiveregex": ["fqdn=^web"]}


def created(entry: dict[str, Any]) -> dict[str, Any]:
    return {"result": entry, "value": "webservers", "summary": 'Added "webservers"'}


def member_added(
    completed: int, hosts: list[list[str]] | None = None, hostgroups: list[list[str]] | None = None
) -> dict[str, Any]:
    return {
        "result": MEMBERS,
        "failed": {"member": {"host": hosts or [], "hostgroup": hostgroups or []}},
        "completed": completed,
    }


def condition_added(completed: int, inclusive: list[str] | None = None) -> dict[str, Any]:
    return {
        "result": CONDITIONS,
        "value": "webservers",
        "summary": 'Added condition(s) to "webservers"',
        "failed": {
            "failed": {"automemberinclusiveregex": inclusive or [], "automemberexclusiveregex": []}
        },
        "completed": completed,
    }


def deleted(name: str, failed: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"result": {"failed": failed or []}, "value": [name], "summary": None, **extra}


#: (op id, params, command, arguments, options, answer, result) for every write.
_WRITES: list[tuple[str, dict[str, Any], str, list[Any], dict[str, Any], Any, Any]] = [
    (
        "freeipa.hostgroup.create",
        {"name": "webservers", "description": "Web servers"},
        "hostgroup_add",
        ["webservers"],
        _options(description="Web servers"),
        created(HOSTGROUP),
        {"entry": HOSTGROUP},
    ),
    (
        "freeipa.hostgroup.create",
        {"name": "webservers"},
        "hostgroup_add",
        ["webservers"],
        _options(),
        created(HOSTGROUP),
        {"entry": HOSTGROUP},
    ),
    (
        "freeipa.hostgroup.member.add",
        {
            "name": "webservers",
            "hosts": ["web01.example.test", "web02.example.test"],
            "hostgroups": ["dbservers"],
        },
        "hostgroup_add_member",
        ["webservers"],
        _options(host=["web01.example.test", "web02.example.test"], hostgroup=["dbservers"]),
        member_added(3),
        {"entry": MEMBERS, "completed": 3},
    ),
    (
        "freeipa.hostgroup.member.add",
        {"name": "webservers", "hostgroups": ["dbservers"]},
        "hostgroup_add_member",
        ["webservers"],
        _options(hostgroup=["dbservers"]),
        member_added(1),
        {"entry": MEMBERS, "completed": 1},
    ),
    (
        "freeipa.automember.rule.create",
        {"hostgroup": "webservers"},
        "automember_add",
        ["webservers"],
        _options(type="hostgroup"),
        created(RULE),
        {"entry": RULE},
    ),
    (
        "freeipa.automember.condition.add",
        {
            "hostgroup": "webservers",
            "key": "fqdn",
            "inclusive_regex": ["^web"],
            "exclusive_regex": ["^webtest"],
        },
        "automember_add_condition",
        ["webservers"],
        _options(
            type="hostgroup",
            key="fqdn",
            automemberinclusiveregex=["^web"],
            automemberexclusiveregex=["^webtest"],
        ),
        condition_added(2),
        {"entry": CONDITIONS, "completed": 2},
    ),
    (
        "freeipa.host.delete",
        {"fqdn": "web01.example.test"},
        "host_del",
        ["web01.example.test"],
        _options(updatedns=False),
        deleted("web01.example.test"),
        {"deleted": ["web01.example.test"], "warnings": []},
    ),
    (
        "freeipa.host.delete",
        {"fqdn": "web01.example.test", "updatedns": True},
        "host_del",
        ["web01.example.test"],
        _options(updatedns=True),
        deleted("web01.example.test"),
        {"deleted": ["web01.example.test"], "warnings": []},
    ),
    (
        "freeipa.hostgroup.delete",
        {"name": "webservers"},
        "hostgroup_del",
        ["webservers"],
        _options(),
        deleted("webservers"),
        {"deleted": ["webservers"], "warnings": []},
    ),
]


def test_every_write_is_covered_here() -> None:
    assert {row[0] for row in _WRITES} == {op.op_id for op in HOST_WRITE_OPS}


def test_writes_wait_for_approval_and_deletes_are_dangerous() -> None:
    assert {op.op_id: op.safety_level for op in HOST_WRITE_OPS} == {
        "freeipa.hostgroup.create": "caution",
        "freeipa.hostgroup.member.add": "caution",
        "freeipa.automember.rule.create": "caution",
        "freeipa.automember.condition.add": "caution",
        "freeipa.host.delete": "dangerous",
        "freeipa.hostgroup.delete": "dangerous",
    }
    for op in HOST_WRITE_OPS:
        assert op.requires_approval is True, op.op_id
        assert "write" in op.tags and "read-only" not in op.tags, op.op_id


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
    ("answer", "named"),
    [
        (
            member_added(1, hosts=[["web99.example.test", "no such entry"]]),
            "host web99.example.test: no such entry",
        ),
        (
            member_added(1, hosts=[["web01.example.test", "This entry is already a member"]]),
            "host web01.example.test: This entry is already a member",
        ),
        (
            member_added(1, hostgroups=[["dbservers", "no such entry"]]),
            "hostgroup dbservers: no such entry",
        ),
    ],
)
async def test_member_add_fails_and_names_each_member_freeipa_did_not_add(
    _stub_embedding: AsyncMock, session: AsyncSession, answer: dict[str, Any], named: str
) -> None:
    await register()
    params = {"name": "webservers", "hosts": ["web01.example.test", "web99.example.test"]}
    result, _, _ = await run(
        "freeipa.hostgroup.member.add", params, {"hostgroup_add_member": answer}, approved=True
    )

    assert result.status == "error"
    rendered = _rendered(result)
    assert "hostgroup_add_member" in rendered
    assert "1 of 2" in rendered
    assert named in rendered


@pytest.mark.asyncio
async def test_member_add_without_a_completed_count_fails(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    answer = {"result": MEMBERS, "failed": {"member": {"host": [], "hostgroup": []}}}
    params = {"name": "webservers", "hosts": ["web01.example.test"]}
    result, _, _ = await run(
        "freeipa.hostgroup.member.add", params, {"hostgroup_add_member": answer}, approved=True
    )
    assert result.status == "error"
    assert "0 of 1" in _rendered(result)


@pytest.mark.asyncio
async def test_condition_add_fails_when_freeipa_adds_fewer_than_sent(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    params = {"hostgroup": "webservers", "key": "fqdn", "inclusive_regex": ["^web"]}
    result, _, _ = await run(
        "freeipa.automember.condition.add",
        params,
        {"automember_add_condition": condition_added(0)},
        approved=True,
    )
    assert result.status == "error"
    rendered = _rendered(result)
    assert "0 of 1" in rendered
    assert "already there" in rendered


@pytest.mark.asyncio
async def test_condition_add_names_a_failed_condition(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    params = {"hostgroup": "webservers", "key": "fqdn", "inclusive_regex": ["^web"]}
    answer = condition_added(1, inclusive=["fqdn=^web"])
    result, _, _ = await run(
        "freeipa.automember.condition.add",
        params,
        {"automember_add_condition": answer},
        approved=True,
    )
    assert result.status == "error"
    assert "automemberinclusiveregex fqdn=^web" in _rendered(result)


@pytest.mark.asyncio
async def test_delete_with_a_failed_name_fails(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    answer = deleted("webservers", failed=["webservers"])
    result, _, _ = await run(
        "freeipa.hostgroup.delete", {"name": "webservers"}, {"hostgroup_del": answer}, approved=True
    )
    assert result.status == "error"
    assert "Not done: webservers" in _rendered(result)


@pytest.mark.asyncio
async def test_host_delete_returns_freeipa_warnings(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    warning = "DNS record(s) of host web01.example.test could not be removed."
    answer = deleted(
        "web01.example.test",
        messages=[{"type": "warning", "name": "FailedToRemoveHostDNSRecords", "message": warning}],
    )
    params = {"fqdn": "web01.example.test", "updatedns": True}
    result, _, _ = await run("freeipa.host.delete", params, {"host_del": answer}, approved=True)
    assert result.status == "ok", result.error
    assert result.result == {"deleted": ["web01.example.test"], "warnings": [warning]}


@pytest.mark.asyncio
async def test_command_error_of_a_write_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    refused = error_answer("ProtectedEntryError", 4309, "ipaservers cannot be deleted")
    params = {"name": "ipaservers"}
    result, _, _ = await run(
        "freeipa.hostgroup.delete", params, {"hostgroup_del": refused}, approved=True
    )
    assert result.status == "error"
    assert "ProtectedEntryError" in _rendered(result)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def _approval_request(result: Any) -> ApprovalRequest:
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, UUID(result.extras["approval_request_id"]))
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_host_delete_waits_for_approval_and_shows_the_host_groups(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, calls, _ = await run("freeipa.host.delete", {"fqdn": "web01.example.test"})

    assert result.status == "awaiting_approval", result.error
    assert [call["body"]["method"] for call in calls] == ["host_show"]
    effect = (await _approval_request(result)).proposed_effect
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"] == {
        "host": "web01.example.test",
        "hostgroups": ["webservers"],
        "indirect_hostgroups": [],
        "updatedns": False,
    }


@pytest.mark.asyncio
async def test_host_delete_of_a_missing_host_still_waits_and_says_why_there_is_no_preview(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    missing = error_answer("NotFound", 4001, "web99.example.test: host not found")
    result, calls, _ = await run(
        "freeipa.host.delete", {"fqdn": "web99.example.test"}, {"host_show": missing}
    )

    assert result.status == "awaiting_approval", result.error
    assert [call["body"]["method"] for call in calls] == ["host_show"]
    effect = (await _approval_request(result)).proposed_effect
    assert effect["preview_unavailable"] is True
    assert "host not found" in effect["preview_error"]


@pytest.mark.asyncio
async def test_a_change_waits_for_approval_before_any_call(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, calls, login = await run("freeipa.hostgroup.create", {"name": "webservers"})

    assert result.status == "awaiting_approval", result.error
    assert calls == []
    assert login.call_count == 0
    effect = (await _approval_request(result)).proposed_effect
    assert effect["safety_level"] == "caution"
    assert effect["params_echo"] == {"name": "webservers"}


@pytest.mark.asyncio
async def test_host_delete_preview_declines_without_a_live_connector() -> None:
    ctx = PreviewContext(
        descriptor=EndpointDescriptor(op_id="freeipa.host.delete"),
        connector_instance=None,
        operator=make_operator(),
        target=Target(),
        params={"fqdn": "web01.example.test"},
    )
    assert await host_delete_preview(ctx) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("freeipa.hostgroup.create", {}),
        ("freeipa.hostgroup.member.add", {"name": "webservers"}),
        ("freeipa.hostgroup.member.add", {"name": "webservers", "hosts": []}),
        (
            "freeipa.hostgroup.member.add",
            {"name": "webservers", "hosts": ["web01.example.test", "web01.example.test"]},
        ),
        ("freeipa.automember.condition.add", {"hostgroup": "webservers", "key": "fqdn"}),
        ("freeipa.automember.condition.add", {"hostgroup": "webservers", "inclusive_regex": ["^"]}),
        ("freeipa.host.delete", {"fqdn": "web01.example.test", "continue": True}),
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
