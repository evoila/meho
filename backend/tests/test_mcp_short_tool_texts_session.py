# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One session asks for the short tool texts with ``?texts=short`` (#6293).

These tests check:

* reading the ``texts`` URL value: blank means the default, ``short`` asks
  for the short texts, any other value is refused;
* with the ``MCP_SHORT_TOOL_TEXTS`` switch off, a session that asked sees
  the short texts with the same inputs, and a session that did not ask sees
  every tool as before;
* with the switch on, every session sees the short texts;
* two sessions at the same time each see their own texts;
* ``tools/call`` checks the call against the texts the session listed;
* the ``tools/call`` audit row names the choice;
* the request usage event names the choice, also when
  ``AUDIT_WRAPPER_ROWS_RETIRED`` leaves out the audit row;
* a profile and the short texts work together, also over HTTP.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app as production_app
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.handlers import handle_tools_call, handle_tools_list
from meho_backplane.mcp.profiles import OPERATE_TOOLS
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE, ToolDefinition, get_tool
from meho_backplane.mcp.schemas import INVALID_REQUEST
from meho_backplane.mcp.short_texts import (
    UnknownToolTextsError,
    parse_tool_texts,
    short_texts_shown,
)
from meho_backplane.mcp.tools._short_tool_texts import CALL_OPERATION_SHORT
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)
from tests.test_mcp_short_tool_texts import SHORT_TEXTS, _without_descriptions

from ._request_usage_helpers import read_usage_events


def _set_switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("MCP_SHORT_TOOL_TEXTS", "true" if on else "false")
    get_settings.cache_clear()


def _operator(*, short: bool = False, profile: str | None = None) -> Operator:
    """An elevated tenant admin with the docs capability: every tool lists."""
    return Operator(
        sub="op-short-texts-session",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        capabilities=frozenset({"meho-docs"}),
        scopes=frozenset({MCP_ADMIN_SCOPE}),
        mcp_tool_profile=profile,
        mcp_short_tool_texts=short,
    )


async def _listed(operator: Operator) -> dict[str, dict[str, Any]]:
    result = await handle_tools_list(operator, None)
    return {tool["name"]: tool for tool in result["tools"]}


# ---------------------------------------------------------------------------
# Reading the URL value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("values", [[], [""], ["  "]])
def test_no_or_blank_value_means_the_default_texts(values: list[str]) -> None:
    assert parse_tool_texts(values) is False


@pytest.mark.parametrize("values", [["short"], ["SHORT"], [" Short "], ["short", "short"]])
def test_short_is_read(values: list[str]) -> None:
    assert parse_tool_texts(values) is True


@pytest.mark.parametrize("values", [["full"], ["long"], ["short", "full"], ["yes"]])
def test_other_values_are_refused(values: list[str]) -> None:
    with pytest.raises(UnknownToolTextsError) as exc_info:
        parse_tool_texts(values)
    assert "unknown tool texts" in str(exc_info.value)
    assert "texts=short" in str(exc_info.value)


def test_error_text_cuts_a_long_value() -> None:
    with pytest.raises(UnknownToolTextsError) as exc_info:
        parse_tool_texts(["x" * 500])
    assert "x" * 65 not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Which session sees the short texts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("switch", "asked", "expected"),
    [(False, False, False), (False, True, True), (True, False, True), (True, True, True)],
)
def test_short_texts_shown(
    monkeypatch: pytest.MonkeyPatch, switch: bool, asked: bool, expected: bool
) -> None:
    _set_switch(monkeypatch, on=switch)
    assert short_texts_shown(_operator(short=asked)) is expected


async def test_switch_off_a_session_that_asked_sees_the_short_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 14 tools show their short text with the same inputs; no other tool changes."""
    _set_switch(monkeypatch, on=False)
    full = await _listed(_operator())
    short = await _listed(_operator(short=True))
    assert set(full) == set(short)
    changed = {name for name in full if full[name] != short[name]}
    assert changed == set(SHORT_TEXTS)
    for name, text in SHORT_TEXTS.items():
        assert short[name]["description"].endswith(text.description), name
        assert _without_descriptions(short[name]["inputSchema"]) == _without_descriptions(
            full[name]["inputSchema"]
        ), name


async def test_session_choice_matches_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asking for the short texts gives the same list as the switch on."""
    _set_switch(monkeypatch, on=False)
    asked = await _listed(_operator(short=True))
    _set_switch(monkeypatch, on=True)
    switched = await _listed(_operator())
    assert asked == switched


async def test_two_sessions_at_the_same_time_each_see_their_own_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_switch(monkeypatch, on=False)
    full, short = await asyncio.gather(_listed(_operator()), _listed(_operator(short=True)))
    registered = get_tool("call_operation")
    assert registered is not None
    assert full["call_operation"]["description"] == registered[0].description
    assert short["call_operation"]["description"] == CALL_OPERATION_SHORT.description


async def test_profile_and_short_texts_work_together(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_switch(monkeypatch, on=False)
    listed = await _listed(_operator(short=True, profile="operate"))
    assert set(listed) == OPERATE_TOOLS
    assert listed["call_operation"]["description"] == CALL_OPERATION_SHORT.description


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asked", [False, True])
async def test_tools_call_uses_the_session_texts_and_audits_the_choice(
    monkeypatch: pytest.MonkeyPatch, asked: bool
) -> None:
    _set_switch(monkeypatch, on=False)
    entry = get_tool("call_operation")
    assert entry is not None
    handler = AsyncMock(return_value={"ok": True})
    checked: list[ToolDefinition] = []
    real = mcp_registry.tool_for_settings

    def spy(definition: ToolDefinition, operator: Operator) -> ToolDefinition:
        shown = real(definition, operator)
        checked.append(shown)
        return shown

    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], handler)),
        patch("meho_backplane.mcp.handlers.tool_for_settings", new=spy),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()) as audit,
    ):
        await handle_tools_call(
            _operator(short=asked),
            {
                "name": "call_operation",
                "arguments": {"connector_id": "<connector-id>", "op_id": "<op-id>"},
            },
        )
    assert [shown.description for shown in checked] == [
        CALL_OPERATION_SHORT.description if asked else entry[0].description
    ]
    assert audit.await_args is not None
    payload = audit.await_args.kwargs["payload"]
    if asked:
        assert payload["tool_texts"] == "short"
    else:
        assert "tool_texts" not in payload


# ---------------------------------------------------------------------------
# Over HTTP: the ``texts`` URL parameter
# ---------------------------------------------------------------------------


_TOOLS_LIST: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _tools(client: TestClient, url: str) -> dict[str, dict[str, Any]]:
    tools = client.post(url, json=_TOOLS_LIST).json()["result"]["tools"]
    return {tool["name"]: tool for tool in tools}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_http_texts_parameter(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_switch(monkeypatch, on=False)
    client, _op = client_with_operator
    full = _tools(client, "/mcp")
    short = _tools(client, "/mcp?texts=short")
    assert full == _tools(client, "/mcp?texts=")
    assert set(full) == set(short)
    assert short["call_operation"]["description"] == CALL_OPERATION_SHORT.description
    assert full["call_operation"]["description"] != CALL_OPERATION_SHORT.description
    # A later request without the parameter gets the full texts again.
    assert _tools(client, "/mcp") == full
    both = _tools(client, "/mcp?profile=operate&texts=short")
    assert set(both) == OPERATE_TOOLS
    assert both["call_operation"] == short["call_operation"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("query", ["texts=full", "texts=short&texts=long"])
def test_http_unknown_texts_is_a_bad_request(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    query: str,
) -> None:
    client, _op = client_with_operator
    response = client.post(f"/mcp?{query}", json=_TOOLS_LIST)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == INVALID_REQUEST
    assert "unknown tool texts" in error["message"]


# ---------------------------------------------------------------------------
# The usage event names the choice (also with the wrapper audit rows retired)
# ---------------------------------------------------------------------------


@pytest.fixture
def retired_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app with usage events on and the wrapper audit rows retired."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", "true")
    _set_switch(monkeypatch, on=False)
    op = build_operator(TenantRole.OPERATOR)

    async def _fake_verify() -> Operator:
        # The usage event names the caller from these context values.
        structlog.contextvars.bind_contextvars(operator_sub=op.sub, tenant_id=str(op.tenant_id))
        return op

    production_app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(production_app) as client:
            yield client
    finally:
        production_app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        get_settings.cache_clear()


@pytest.mark.parametrize(("url", "expected"), [("/mcp?texts=short", "short"), ("/mcp", None)])
async def test_usage_event_names_the_choice_when_no_audit_row_is_written(
    retired_client: TestClient, url: str, expected: str | None
) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "meho_status", "arguments": {}},
    }
    response = retired_client.post(url, json=call)
    assert response.status_code == 200
    assert "error" not in response.json()

    [event] = [e for e in await read_usage_events() if e.transport == "mcp"]
    assert event.route == "tool:meho_status"
    assert event.tool_texts == expected
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.method == "MCP"))).all()
    # A read writes no wrapper audit row, so the usage event is the only record.
    assert rows == []
