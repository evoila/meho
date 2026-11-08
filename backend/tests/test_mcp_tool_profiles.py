# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tool profiles: a session asks for a smaller tool list (#4172).

The client adds ``?profile=<name>`` to the MCP URL. These tests check:

* the URL value is read strictly: an unknown name or two names are
  refused, blank means no profile;
* ``tools/list`` shows only the profile's tools, and a profile never adds
  a tool the role, capability or surface gates hide;
* ``tools/call`` refuses a tool outside the profile, and the handler does
  not run;
* the session-start instructions name no tool the profile hides;
* no profile keeps today's full list.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.conventions.preamble import (
    BROADCAST_BLOCK_START,
    PreambleAssembly,
    assemble_preamble_detailed,
)
from meho_backplane.mcp.handlers import handle_tools_call, handle_tools_list
from meho_backplane.mcp.profiles import (
    INVESTIGATE_TOOLS,
    OPERATE_TOOLS,
    TOOL_PROFILES,
    UnknownToolProfileError,
    parse_tool_profile,
    profile_admits,
)
from meho_backplane.mcp.registry import (
    MCP_ADMIN_SCOPE,
    ToolSurface,
    all_tools_for,
    get_tool,
)
from meho_backplane.mcp.schemas import INVALID_REQUEST, PROTOCOL_VERSION
from meho_backplane.mcp.server import McpInvalidParamsError, _session_instructions
from meho_backplane.runbooks.priming import RunbookPrimingResult
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


def _operator(
    role: TenantRole = TenantRole.TENANT_ADMIN,
    *,
    profile: str | None = None,
    elevated: bool = False,
) -> Operator:
    """An operator with the docs capability, optionally elevated and in a profile."""
    return Operator(
        sub="op-profile",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=role,
        capabilities=frozenset({"meho-docs"}),
        scopes=frozenset({MCP_ADMIN_SCOPE}) if elevated else frozenset(),
        mcp_tool_profile=profile,
    )


def _listed(operator: Operator) -> set[str]:
    return {defn.name for defn in all_tools_for(operator)}


# ---------------------------------------------------------------------------
# Reading the URL value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("values", [[], [""], ["  "], ["", " "]])
def test_no_or_blank_profile_means_full_list(values: list[str]) -> None:
    assert parse_tool_profile(values) is None


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["investigate"], "investigate"),
        (["operate"], "operate"),
        ([" Investigate "], "investigate"),
        (["OPERATE", "operate"], "operate"),
    ],
)
def test_known_profile_is_read(values: list[str], expected: str) -> None:
    assert parse_tool_profile(values) == expected


@pytest.mark.parametrize(
    "values",
    [["full"], ["admin"], ["investigate", "operate"], ["investigate", "nope"]],
)
def test_unknown_or_two_profiles_are_refused(values: list[str]) -> None:
    with pytest.raises(UnknownToolProfileError) as exc_info:
        parse_tool_profile(values)
    message = str(exc_info.value)
    assert "investigate, operate" in message
    assert "full tool list" in message


def test_error_text_cuts_a_long_name() -> None:
    with pytest.raises(UnknownToolProfileError) as exc_info:
        parse_tool_profile(["x" * 5_000])
    assert len(str(exc_info.value)) < 300


def test_unknown_profile_on_an_operator_keeps_no_tool() -> None:
    """A profile name that skipped the URL check fails closed."""
    assert profile_admits("not-a-profile", "meho_status") is False
    assert profile_admits(None, "meho_admin_anything") is True


# ---------------------------------------------------------------------------
# The profile lists
# ---------------------------------------------------------------------------


def test_every_profile_tool_is_a_registered_working_tool() -> None:
    """A typo in a profile list fails here, not in a client."""
    for name, tools in TOOL_PROFILES.items():
        for tool in tools:
            entry = get_tool(tool)
            assert entry is not None, f"{name}: {tool!r} is not registered"
            assert entry[0].surface is ToolSurface.WORKING, f"{name}: {tool!r}"


def test_operate_is_investigate_plus_preview_and_broadcast() -> None:
    assert OPERATE_TOOLS > INVESTIGATE_TOOLS
    added = OPERATE_TOOLS - INVESTIGATE_TOOLS
    assert added == {"preview_operation", "meho_broadcast_recent", "meho_broadcast_announce"}
    # The investigate profile has no tool that writes to MEHO itself.
    for tool in INVESTIGATE_TOOLS - {"call_operation"}:
        entry = get_tool(tool)
        assert entry is not None
        assert entry[0].op_class == "read", tool


@pytest.mark.parametrize("profile", sorted(TOOL_PROFILES))
@pytest.mark.parametrize("elevated", [False, True])
def test_profile_lists_exactly_its_tools(profile: str, elevated: bool) -> None:
    """Admin elevation and the docs add-on add nothing to a profile."""
    assert _listed(_operator(profile=profile, elevated=elevated)) == TOOL_PROFILES[profile]


def test_profile_only_removes_tools() -> None:
    """A read_only person in the operate profile still sees only meho_status."""
    full = _listed(_operator(TenantRole.READ_ONLY))
    in_profile = _listed(_operator(TenantRole.READ_ONLY, profile="operate"))
    assert in_profile <= full
    assert in_profile == {"meho_status"}


def test_no_profile_keeps_the_full_list() -> None:
    full = _listed(_operator())
    assert full > INVESTIGATE_TOOLS
    assert "query_topology" in full
    assert "add_to_memory" in full


def test_profiles_are_much_smaller_than_the_full_list() -> None:
    """The point of a profile: far less tool text before the question."""
    full = all_tools_for(_operator())
    for profile in TOOL_PROFILES:
        tools = all_tools_for(_operator(profile=profile))
        assert len(tools) * 2 <= len(full), profile


# ---------------------------------------------------------------------------
# tools/list and tools/call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_handler_follows_the_profile() -> None:
    result = await handle_tools_list(_operator(profile="investigate"), None)
    assert {tool["name"] for tool in result["tools"]} == INVESTIGATE_TOOLS


@pytest.mark.asyncio
async def test_call_to_a_tool_outside_the_profile_is_refused() -> None:
    entry = get_tool("add_to_memory")
    assert entry is not None
    handler = AsyncMock(return_value={})
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()) as audit,
        pytest.raises(McpInvalidParamsError) as exc_info,
    ):
        await handle_tools_call(
            _operator(profile="investigate"),
            {"name": "add_to_memory", "arguments": {}},
        )
    handler.assert_not_awaited()
    assert "not in the 'investigate' tool profile" in str(exc_info.value)
    assert exc_info.value.data == {"reason": "not_in_profile", "profile": "investigate"}
    # The refused call is still audited, as a 403.
    assert audit.await_args is not None
    assert audit.await_args.kwargs["status_code"] == 403


@pytest.mark.asyncio
async def test_call_to_a_tool_in_the_profile_runs() -> None:
    entry = get_tool("meho_status")
    assert entry is not None
    handler = AsyncMock(return_value={"ok": True})
    with (
        patch("meho_backplane.mcp.handlers.get_tool", return_value=(entry[0], handler)),
        patch("meho_backplane.mcp.handlers.write_mcp_audit_row", new=AsyncMock()),
    ):
        await handle_tools_call(
            _operator(profile="investigate"),
            {"name": "meho_status", "arguments": {}},
        )
    handler.assert_awaited_once()


# ---------------------------------------------------------------------------
# Over HTTP: the ``profile`` URL parameter
# ---------------------------------------------------------------------------


def _post(client: TestClient, url: str, body: dict[str, Any]) -> Any:
    return client.post(url, json=body)


_TOOLS_LIST: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_http_profile_parameter_cuts_the_list(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    full = _post(client, "/mcp", _TOOLS_LIST).json()["result"]["tools"]
    cut = _post(client, "/mcp?profile=operate", _TOOLS_LIST).json()["result"]["tools"]
    assert {tool["name"] for tool in cut} == OPERATE_TOOLS
    assert len(full) > len(cut)
    blank = _post(client, "/mcp?profile=", _TOOLS_LIST).json()["result"]["tools"]
    assert blank == full


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.parametrize("query", ["profile=full", "profile=investigate&profile=operate"])
def test_http_unknown_profile_is_a_bad_request(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    query: str,
) -> None:
    client, _op = client_with_operator
    response = _post(client, f"/mcp?{query}", _TOOLS_LIST)
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == INVALID_REQUEST
    assert "unknown tool profile" in error["message"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_http_call_outside_the_profile_is_refused(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = _post(
        client,
        "/mcp?profile=investigate",
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "query_topology", "arguments": {"kind": "edges"}},
        },
    )
    error = response.json()["error"]
    assert error["data"]["reason"] == "not_in_profile"


# ---------------------------------------------------------------------------
# Session-start instructions
# ---------------------------------------------------------------------------


def _initialize() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-harness", "version": "0.0.0"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
async def test_investigate_instructions_name_no_broadcast_tool(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    seeded_operator_tenant: None,  # noqa: F811
) -> None:
    client, _op = client_with_operator
    plain = _post(client, "/mcp", _initialize()).json()["result"]["instructions"]
    investigate = _post(client, "/mcp?profile=investigate", _initialize()).json()["result"][
        "instructions"
    ]
    operate = _post(client, "/mcp?profile=operate", _initialize()).json()["result"]["instructions"]
    assert BROADCAST_BLOCK_START in plain
    assert BROADCAST_BLOCK_START in operate
    assert BROADCAST_BLOCK_START not in investigate
    assert "meho_broadcast_" not in investigate


def _fake_preamble() -> PreambleAssembly:
    return PreambleAssembly(
        text="",
        dropped_slugs=[],
        kept_slugs=[],
        token_counts={},
        conventions_tokens=0,
        oversize_slugs=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "expect_bands"),
    [(None, True), ("investigate", False), ("operate", False)],
)
async def test_instructions_drop_runbook_and_docs_bands_outside_the_profile(
    profile: str | None,
    expect_bands: bool,
) -> None:
    """Both profiles hide the runbook and docs tools, so their bands go too."""
    assemble = AsyncMock(return_value=_fake_preamble())
    with (
        patch("meho_backplane.conventions.preamble.assemble_preamble_detailed", new=assemble),
        patch("meho_backplane.recall.memory_band", new=AsyncMock(return_value="")),
    ):
        await _session_instructions(_operator(TenantRole.OPERATOR, profile=profile))
    kwargs = assemble.await_args.kwargs
    assert kwargs["include_runbook_priming"] is expect_bands
    assert (kwargs["capabilities"] is not None) is expect_bands


@pytest.mark.asyncio
async def test_preamble_skips_runbook_priming_when_asked() -> None:
    priming = AsyncMock(
        return_value=RunbookPrimingResult(
            text="call meho_runbook_next", block_count=1, summarized=False
        )
    )
    with (
        patch("meho_backplane.conventions.preamble.assemble_runbook_priming", new=priming),
        patch(
            "meho_backplane.conventions.preamble._load_conventions",
            new=AsyncMock(return_value=[]),
        ),
    ):
        skipped = await assemble_preamble_detailed(
            OPERATOR_TENANT_ID,
            "op-profile",
            include_broadcast=False,
            include_runbook_priming=False,
        )
        kept = await assemble_preamble_detailed(
            OPERATOR_TENANT_ID,
            "op-profile",
            include_broadcast=False,
        )
    assert priming.await_count == 1
    assert skipped.text == ""
    assert skipped.runbook_block_count == 0
    assert "meho_runbook_next" in kept.text
    assert kept.runbook_block_count == 1
