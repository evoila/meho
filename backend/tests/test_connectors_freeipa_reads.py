# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the seventeen FreeIPA reads, through ``dispatch()``.

Each read sends one IPA command. What is covered: the command, its arguments
and options as sent, and the shape the read returns (``{rows, total,
truncated}`` for lists, ``{entry}`` for one object, the mapped fields of
``freeipa.hbac.test``). Dates come back as ISO 8601 text.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.freeipa import (
    FREEIPA_IMPL_ID,
    FREEIPA_PRODUCT,
    FREEIPA_TYPED_OPS,
    FreeipaConnector,
)
from meho_backplane.connectors.freeipa.reads import DEFAULT_LIMIT
from meho_backplane.connectors.freeipa.rpc import API_VERSION, decode_values
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings
from tests._freeipa_fixtures import (
    ALL_ANSWERS,
    FREEIPA_VERSION,
    HBACTEST,
    HOST_ROW,
    find_answer,
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


#: (op id, params, command, arguments, options) for every list read.
_LIST_READS: list[tuple[str, dict[str, Any], str, list[Any], dict[str, Any]]] = [
    ("freeipa.host.list", {}, "host_find", [], _options(sizelimit=DEFAULT_LIMIT)),
    (
        "freeipa.host.list",
        {"text": "web", "hostgroup": "webservers", "limit": 5},
        "host_find",
        ["web"],
        _options(sizelimit=5, in_hostgroup=["webservers"]),
    ),
    ("freeipa.hostgroup.list", {"limit": 7}, "hostgroup_find", [], _options(sizelimit=7)),
    ("freeipa.user.list", {"text": "jdoe"}, "user_find", ["jdoe"], _options(sizelimit=100)),
    ("freeipa.group.list", {}, "group_find", [], _options(sizelimit=100)),
    ("freeipa.role.list", {}, "role_find", [], _options(sizelimit=100)),
    ("freeipa.sudorule.list", {}, "sudorule_find", [], _options(sizelimit=100)),
    ("freeipa.hbacrule.list", {}, "hbacrule_find", [], _options(sizelimit=100)),
]

#: (op id, params, command, arguments) for every read of one object.
_SHOW_READS: list[tuple[str, dict[str, Any], str, list[Any]]] = [
    ("freeipa.host.show", {"fqdn": "web01.example.test"}, "host_show", ["web01.example.test"]),
    ("freeipa.hostgroup.show", {"name": "webservers"}, "hostgroup_show", ["webservers"]),
    ("freeipa.user.show", {"uid": "jdoe"}, "user_show", ["jdoe"]),
    ("freeipa.group.show", {"name": "admins"}, "group_show", ["admins"]),
    (
        "freeipa.role.show",
        {"name": "Enrollment Administrator"},
        "role_show",
        ["Enrollment Administrator"],
    ),
    ("freeipa.sudorule.show", {"name": "admins-all"}, "sudorule_show", ["admins-all"]),
    ("freeipa.hbacrule.show", {"name": "allow_all"}, "hbacrule_show", ["allow_all"]),
    ("freeipa.pwpolicy.show", {}, "pwpolicy_show", []),
    ("freeipa.pwpolicy.show", {"group": "admins"}, "pwpolicy_show", ["admins"]),
]


def test_every_read_is_covered_here() -> None:
    covered = {row[0] for row in _LIST_READS} | {row[0] for row in _SHOW_READS}
    covered |= {"freeipa.about", "freeipa.hbac.test"}
    assert covered == {op.op_id for op in FREEIPA_TYPED_OPS}


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params", "command", "args", "options"), _LIST_READS)
async def test_list_read_sends_its_find_command(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
    args: list[Any],
    options: dict[str, Any],
) -> None:
    await register()
    result, calls, _ = await run(op_id, params)

    assert result.status == "ok", result.error
    assert [call["body"] for call in calls] == [
        {"method": command, "params": [args, options], "id": 0}
    ]
    expected_rows = ALL_ANSWERS[command]["result"]
    assert result.result["total"] == len(expected_rows)
    assert result.result["truncated"] is False
    assert len(result.result["rows"]) == len(expected_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(("op_id", "params", "command", "args"), _SHOW_READS)
async def test_show_read_sends_its_show_command(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    command: str,
    args: list[Any],
) -> None:
    await register()
    result, calls, _ = await run(op_id, params)

    assert result.status == "ok", result.error
    assert [call["body"] for call in calls] == [
        {"method": command, "params": [args, _options()], "id": 0}
    ]
    assert result.result == {"entry": decode_values(ALL_ANSWERS[command]["result"])}


@pytest.mark.asyncio
async def test_host_list_decodes_dates_and_keeps_freeipa_attributes(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    result, _, _ = await run("freeipa.host.list", {})
    assert result.status == "ok", result.error
    row = result.result["rows"][0]
    assert row["fqdn"] == HOST_ROW["fqdn"]
    assert row["memberof_hostgroup"] == ["webservers"]
    assert row["krblastpwdchange"] == ["2026-10-01T12:00:00Z"]


@pytest.mark.asyncio
async def test_list_read_reports_a_truncated_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    answers = {"user_find": find_answer([{"uid": ["a"]}, {"uid": ["b"]}], truncated=True)}
    result, _, _ = await run("freeipa.user.list", {"limit": 2}, answers)
    assert result.status == "ok", result.error
    assert (result.result["total"], result.result["truncated"]) == (2, True)


@pytest.mark.asyncio
async def test_hbac_test_sends_user_host_service_and_maps_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    params = {
        "user": "jdoe",
        "host": "web01.example.test",
        "service": "sshd",
        "rules": ["allow_ssh_web"],
    }
    result, calls, _ = await run("freeipa.hbac.test", params)

    assert result.status == "ok", result.error
    assert calls[0]["body"]["method"] == "hbactest"
    assert calls[0]["body"]["params"] == [
        [],
        _options(
            user="jdoe", targethost="web01.example.test", service="sshd", rules=["allow_ssh_web"]
        ),
    ]
    assert result.result == {
        "granted": True,
        "matched": HBACTEST["matched"],
        "notmatched": HBACTEST["notmatched"],
        "warnings": [],
        "errors": [],
    }


@pytest.mark.asyncio
async def test_hbac_test_reports_denied_access(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register()
    denied = {**HBACTEST, "value": False, "matched": None, "notmatched": ["allow_all"]}
    params = {"user": "jdoe", "host": "web01.example.test", "service": "sshd"}
    result, calls, _ = await run("freeipa.hbac.test", params, {"hbactest": denied})

    assert result.status == "ok", result.error
    assert "rules" not in calls[0]["body"]["params"][1]
    assert result.result["granted"] is False
    assert result.result["matched"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("freeipa.host.list", {"limit": 0}),
        ("freeipa.host.list", {"limit": 2001}),
        ("freeipa.host.list", {"text": ""}),
        ("freeipa.host.show", {}),
        ("freeipa.hbac.test", {"user": "jdoe", "host": "web01.example.test"}),
        ("freeipa.user.list", {"unknown": 1}),
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
