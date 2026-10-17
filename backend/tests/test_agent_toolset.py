# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for toolset resolution + the handler-to-agent-tool adapter (G11.1-T3 / #810).

The adapter turns an agent definition's toolset spec plus the run's identity
into the concrete Pydantic AI tools the loop registers. The #810 acceptance
criteria map onto the tests here as:

* ``test_default_registers_all_meta_tools`` / ``test_meta_tools_allow_list_*``
  — the registered set is exactly toolset ∩ identity perms; disallowed tools
  are absent.
* ``test_read_only_role_excludes_operator_floor_tools`` — **the intersection
  test**: an identity whose role is below a tool's floor cannot call that
  tool (it is not registered), even when the spec lists it.
* ``test_wrapped_tool_threads_operator_to_handler`` — each tool call routes
  the run's operator through the handler (so dispatch RBAC + audit + the
  sanitizing reducer fire identically to REST / MCP).
* ``test_tool_input_schema_reflects_meta_tool_schema`` — tool input schemas
  reflect the meta-tool parameter schemas the model is advertised.
* ``test_connector_not_in_allow_list_raises_model_retry`` — a denied
  connector returns a structured, agent-reasonable error (ModelRetry), not a
  crash.

The #3737 tests (governed discovery and result-handle tools) cover: the
pinned configured surface for ``{}`` and the legacy ``None`` two-tool
fallback; missing versus disabled outcomes; the role floor at resolution and
again at call time; the MCP input schema enforced before the handler; the
registered MCP handler reused with the run's operator; a JSONFlux handle
returned unchanged; and the connector allow-list on ``preview_operation``.

The wrapped handlers are stubbed so these tests stay offline (no DB, no
network): the adapter's contract is *which* tools register and *how* they
thread the operator, not what the handlers do once called.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import meho_backplane.agent.toolset as toolset_mod
import meho_backplane.agent.toolset_discovery as toolset_discovery
from meho_backplane.agent import AgentDefinition, PydanticAgentRun
from meho_backplane.agent.toolset import (
    META_TOOL_NAMES,
    RUNBOOK_EXECUTION_META_TOOL_NAMES,
    resolve_agent_tools,
    resolve_agent_toolset,
    toolset_admits_runbook_execution,
)
from meho_backplane.agent.toolset_discovery import DISCOVERY_META_TOOL_NAMES
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolSurface, get_tool

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")


def _make_operator(
    *,
    role: TenantRole = TenantRole.OPERATOR,
    sub: str = "op-agent",
) -> Operator:
    return Operator(
        sub=sub,
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=role,
    )


def _tool_names(tools: list[Any]) -> set[str]:
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Side 1 of the intersection — the toolset spec allow-list
# ---------------------------------------------------------------------------


def test_default_registers_all_meta_tools() -> None:
    """A ``None`` toolset spec registers every meta-tool the role admits."""
    tools = resolve_agent_tools(None, _make_operator())
    assert _tool_names(tools) == set(META_TOOL_NAMES)


def test_empty_spec_dict_registers_all_meta_tools() -> None:
    """An empty spec dict (no ``meta_tools`` key) means 'all role admits'.

    Distinct from a spec with ``meta_tools: []`` — an *omitted* allow-list is
    the 'no restriction' sentinel, an empty list is 'restrict to nothing'.
    """
    tools = resolve_agent_tools({}, _make_operator())
    assert _tool_names(tools) == set(META_TOOL_NAMES)


def test_meta_tools_allow_list_registers_only_listed() -> None:
    """A ``meta_tools`` allow-list registers exactly the listed tools."""
    spec = {"meta_tools": ["list_operation_groups", "call_operation"]}
    tools = resolve_agent_tools(spec, _make_operator())
    assert _tool_names(tools) == {"list_operation_groups", "call_operation"}
    assert "search_operations" not in _tool_names(tools)


def test_empty_meta_tools_list_registers_nothing() -> None:
    """``meta_tools: []`` registers no tools (restrict-to-nothing)."""
    tools = resolve_agent_tools({"meta_tools": []}, _make_operator())
    assert tools == []


def test_unknown_meta_tool_name_is_ignored() -> None:
    """An unknown name in the allow-list is forward-compat noise, not a crash."""
    spec = {"meta_tools": ["call_operation", "some_future_tool"]}
    tools = resolve_agent_tools(spec, _make_operator())
    assert _tool_names(tools) == {"call_operation"}


# ---------------------------------------------------------------------------
# Side 2 of the intersection — identity permissions (THE intersection test)
# ---------------------------------------------------------------------------


def test_read_only_role_excludes_operator_floor_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An op outside the identity's perms is NOT callable (#810 AC).

    The three meta-tools carry an ``OPERATOR`` floor; a ``read_only``
    identity ranks below it, so even with every tool explicitly listed in the
    spec, none register — the intersection (spec ∩ identity perms) is empty.
    To prove the gate is *role-driven* (not a blanket 'read_only gets
    nothing'), one catalog entry is patched down to a ``READ_ONLY`` floor and
    must then register for the same identity.
    """
    spec = {"meta_tools": list(META_TOOL_NAMES)}
    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-read-only")

    # Every catalog tool floors at OPERATOR -> read_only sees nothing.
    assert resolve_agent_tools(spec, read_only) == []

    # An OPERATOR identity, same spec -> full surface (the tools ARE callable
    # for a role that meets the floor).
    operator_tools = resolve_agent_tools(spec, _make_operator())
    assert _tool_names(operator_tools) == set(META_TOOL_NAMES)

    # Drop one tool's floor to READ_ONLY and confirm it now registers for the
    # read_only identity — proving the gate keys off the role, not the tool.
    patched = tuple(
        toolset_mod.MetaToolSpec(
            name=m.name,
            handler=m.handler,
            description=m.description,
            parameter_schema=m.parameter_schema,
            required_role=(
                TenantRole.READ_ONLY if m.name == "list_operation_groups" else m.required_role
            ),
        )
        for m in toolset_mod._META_TOOL_CATALOG
    )
    monkeypatch.setattr(toolset_mod, "_META_TOOL_CATALOG", patched)
    relaxed_tools = resolve_agent_tools(spec, read_only)
    assert _tool_names(relaxed_tools) == {"list_operation_groups"}


def test_tenant_admin_sees_all_operator_floor_tools() -> None:
    """A higher role (tenant_admin) admits every operator-floored tool."""
    admin = _make_operator(role=TenantRole.TENANT_ADMIN, sub="op-admin")
    tools = resolve_agent_tools(None, admin)
    assert _tool_names(tools) == set(META_TOOL_NAMES)


# ---------------------------------------------------------------------------
# Adapter behaviour — operator threading, schema, structured denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapped_tool_threads_operator_to_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapped tool passes the run's operator + repacked args to the handler."""
    seen: dict[str, Any] = {}

    async def fake_list_groups(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        seen["operator_sub"] = operator.sub
        seen["arguments"] = arguments
        return {"groups": []}

    # Patch the catalog entry's handler so no DB is touched.
    patched = tuple(
        toolset_mod.MetaToolSpec(
            name=m.name,
            handler=(fake_list_groups if m.name == "list_operation_groups" else m.handler),
            description=m.description,
            parameter_schema=m.parameter_schema,
            required_role=m.required_role,
        )
        for m in toolset_mod._META_TOOL_CATALOG
    )
    monkeypatch.setattr(toolset_mod, "_META_TOOL_CATALOG", patched)

    operator = _make_operator(sub="op-distinct-principal")
    tools = resolve_agent_tools({"meta_tools": ["list_operation_groups"]}, operator)
    (tool,) = tools

    ctx = _make_run_context(operator)
    result = await tool.function(ctx, connector_id="vault-1.x")

    assert result == {"groups": []}
    assert seen["operator_sub"] == "op-distinct-principal"
    assert seen["arguments"] == {"connector_id": "vault-1.x"}


def test_tool_input_schema_reflects_meta_tool_schema() -> None:
    """The registered tool's JSON schema reflects the meta-tool parameter schema."""
    tools = resolve_agent_tools({"meta_tools": ["call_operation"]}, _make_operator())
    (tool,) = tools
    schema = tool.function_schema.json_schema
    props = schema["properties"]
    # call_operation's canonical arguments are present in the advertised schema.
    assert set(schema["required"]) == {"connector_id", "op_id"}
    assert "params" in props
    assert "target" in props


@pytest.mark.asyncio
async def test_connector_not_in_allow_list_raises_model_retry() -> None:
    """A connector outside the spec's allow-list yields a ModelRetry, not a crash."""
    spec = {"meta_tools": ["call_operation"], "connectors": ["vault-1.x"]}
    operator = _make_operator()
    tools = resolve_agent_tools(spec, operator)
    (tool,) = tools

    ctx = _make_run_context(operator)
    with pytest.raises(ModelRetry, match="not in this agent's allowed connectors"):
        await tool.function(
            ctx,
            connector_id="vmware-rest-9.0",
            op_id="GET:/api/vcenter/cluster",
        )


@pytest.mark.asyncio
async def test_connector_in_allow_list_reaches_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector inside the allow-list flows through to the handler."""
    seen: dict[str, Any] = {}

    async def fake_call_operation(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        seen["connector_id"] = arguments["connector_id"]
        return {"status": "ok", "op_id": arguments["op_id"]}

    patched = tuple(
        toolset_mod.MetaToolSpec(
            name=m.name,
            handler=(fake_call_operation if m.name == "call_operation" else m.handler),
            description=m.description,
            parameter_schema=m.parameter_schema,
            required_role=m.required_role,
        )
        for m in toolset_mod._META_TOOL_CATALOG
    )
    monkeypatch.setattr(toolset_mod, "_META_TOOL_CATALOG", patched)

    spec = {"meta_tools": ["call_operation"], "connectors": ["vault-1.x"]}
    operator = _make_operator()
    tools = resolve_agent_tools(spec, operator)
    (tool,) = tools

    ctx = _make_run_context(operator)
    result = await tool.function(ctx, connector_id="vault-1.x", op_id="vault.kv.read")
    assert result == {"status": "ok", "op_id": "vault.kv.read"}
    assert seen["connector_id"] == "vault-1.x"


def test_empty_connectors_list_forbids_all_connectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connectors: []`` registers call_operation but forbids every dispatch."""

    async def _never(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("handler must not be reached when connector is denied")

    patched = tuple(
        toolset_mod.MetaToolSpec(
            name=m.name,
            handler=(_never if m.name == "call_operation" else m.handler),
            description=m.description,
            parameter_schema=m.parameter_schema,
            required_role=m.required_role,
        )
        for m in toolset_mod._META_TOOL_CATALOG
    )
    monkeypatch.setattr(toolset_mod, "_META_TOOL_CATALOG", patched)

    spec: dict[str, Any] = {"meta_tools": ["call_operation"], "connectors": []}
    operator = _make_operator()
    tools = resolve_agent_tools(spec, operator)
    assert _tool_names(tools) == {"call_operation"}

    import asyncio

    ctx = _make_run_context(operator)
    (tool,) = tools
    with pytest.raises(ModelRetry):
        asyncio.run(tool.function(ctx, connector_id="vault-1.x", op_id="vault.kv.read"))


# ---------------------------------------------------------------------------
# Spec validation
# ---------------------------------------------------------------------------


def test_mis_shaped_meta_tools_raises() -> None:
    """A non-list ``meta_tools`` is a definition-authoring bug -> ValueError."""
    with pytest.raises(ValueError, match="meta_tools"):
        resolve_agent_tools({"meta_tools": "call_operation"}, _make_operator())


def test_mis_shaped_connectors_raises() -> None:
    """A non-list ``connectors`` is a definition-authoring bug -> ValueError."""
    with pytest.raises(ValueError, match="connectors"):
        resolve_agent_tools({"connectors": {"vault-1.x": True}}, _make_operator())


def test_unknown_spec_keys_are_ignored() -> None:
    """An unrecognised spec key is forward-compat tolerated, not rejected."""
    spec = {"meta_tools": ["call_operation"], "future_field": {"x": 1}}
    tools = resolve_agent_tools(spec, _make_operator())
    assert _tool_names(tools) == {"call_operation"}


# ---------------------------------------------------------------------------
# Runbook-execution capability (#2077)
# ---------------------------------------------------------------------------


def test_meta_tool_catalog_has_no_runbook_execution_tool() -> None:
    """The agent↔runbook contract the run-start guard rests on, pinned.

    Runbook execution (``meho_runbook_start`` / ``meho_runbook_next``) is an
    operator MCP surface — confirm-gated steps require a human answer — so
    the agent meta-tool catalog must not (and does not) expose it. A future
    task adding an agent-executable runbook tool must also list it in
    ``RUNBOOK_EXECUTION_META_TOOL_NAMES`` so the guard admits definitions
    that carry it; this test keeps the two sets in lock-step.
    """
    assert RUNBOOK_EXECUTION_META_TOOL_NAMES <= META_TOOL_NAMES
    assert not any("runbook" in name for name in META_TOOL_NAMES)
    assert not RUNBOOK_EXECUTION_META_TOOL_NAMES


@pytest.mark.parametrize(
    "spec",
    [
        None,  # T1 default surface (call_operation + list_operation_groups)
        {},  # the persisted default toolset (the #2077 repro shape)
        {"meta_tools": ["call_operation", "search_operations"]},
        {"meta_tools": []},
        {"meta_tools": "not-a-list"},  # mis-shaped spec answers fail-closed
    ],
)
def test_no_toolset_admits_runbook_execution_today(spec: Any) -> None:
    """With an empty capability catalog every spec answers ``False``."""
    assert toolset_admits_runbook_execution(spec) is False


# ---------------------------------------------------------------------------
# Governed discovery and result-handle tools (#3737)
# ---------------------------------------------------------------------------

#: The configured surface for an empty spec (``{}``), in catalog order. A
#: change here changes what every configured hosted agent can reach, so it is
#: pinned on purpose.
_CONFIGURED_SURFACE: list[str] = [
    "list_operation_groups",
    "search_operations",
    "call_operation",
    "meho_connector_list",
    "list_targets",
    "preview_operation",
    "result_query",
    "broadcast_announce",
    "broadcast_recent",
    "broadcast_watch",
]

_DISCOVERY_TOOLS: frozenset[str] = frozenset(DISCOVERY_META_TOOL_NAMES)


def test_empty_configured_spec_resolves_pinned_surface() -> None:
    """Regression fixture: ``{}`` resolves exactly the pinned configured surface."""
    tools = resolve_agent_tools({}, _make_operator())
    assert [t.name for t in tools] == _CONFIGURED_SURFACE


def test_configured_definition_resolves_search_plus_four_governed_tools() -> None:
    """A configured definition gets ``search_operations`` plus the four adapters."""
    assert {
        "meho_connector_list",
        "list_targets",
        "preview_operation",
        "result_query",
    } == _DISCOVERY_TOOLS
    spec = {"meta_tools": ["search_operations", *DISCOVERY_META_TOOL_NAMES]}
    tools = resolve_agent_tools(spec, _make_operator())
    assert _tool_names(tools) == {"search_operations"} | _DISCOVERY_TOOLS


def test_configured_surface_has_no_runbook_approval_or_admin_tools() -> None:
    """Out of scope by design: no runbook, approval, or operator-plane tool."""
    names = set(META_TOOL_NAMES)
    assert not any("runbook" in name or "approval" in name for name in names)
    for name in names:
        entry = get_tool(name)
        if entry is not None:
            assert entry[0].surface is ToolSurface.WORKING, name


async def test_legacy_none_definition_keeps_two_tool_fallback() -> None:
    """Regression fixture: ``toolset=None`` still gets only the legacy two tools."""
    captured: dict[str, list[str]] = {}

    def inspect_surface(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["tool_names"] = sorted(td.name for td in info.function_tools)
        return ModelResponse(parts=[TextPart("inspected")])

    model = FunctionModel(inspect_surface)
    runtime = PydanticAgentRun(model_factory=lambda: model)
    definition = AgentDefinition(name="legacy", system_prompt="legacy", request_limit=2)
    operator = _make_operator()
    agent = runtime._build_agent(definition, operator)
    await agent.run("which tools?", deps=operator)

    assert captured["tool_names"] == ["call_operation_tool", "list_operation_groups_tool"]


def test_missing_and_disabled_tools_are_reported_apart() -> None:
    """An unknown name is ``missing``; a known but unregistered tool is ``disabled``."""
    spec = {"meta_tools": ["list_targets", "result_query", "no_such_tool"]}
    resolution = resolve_agent_toolset(spec, _make_operator())

    assert [t.name for t in resolution.tools] == ["list_targets", "result_query"]
    assert resolution.missing == ("no_such_tool",)
    assert resolution.disabled["meho_connector_list"] == "not_in_allow_list"
    assert "no_such_tool" not in resolution.disabled

    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-read-only")
    denied = resolve_agent_toolset(spec, read_only)
    assert denied.tools == []
    assert denied.missing == ("no_such_tool",)
    assert denied.disabled["list_targets"] == "role_below_floor"
    assert denied.disabled["result_query"] == "role_below_floor"


def test_read_only_role_gets_no_governed_discovery_tool() -> None:
    """The four adapters keep the MCP tools' OPERATOR floor."""
    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-read-only")
    tools = resolve_agent_tools({}, read_only)
    assert not (_tool_names(tools) & _DISCOVERY_TOOLS)


@pytest.mark.parametrize("name", DISCOVERY_META_TOOL_NAMES)
def test_bridged_tool_matches_mcp_definition(name: str) -> None:
    """Each adapter publishes the MCP wire schema and keeps the MCP role floor."""
    entry = get_tool(name)
    assert entry is not None
    definition, _handler = entry
    (spec,) = [m for m in toolset_mod._META_TOOL_CATALOG if m.name == name]
    assert spec.parameter_schema == definition.to_wire()["inputSchema"]
    assert spec.parameter_schema is not definition.inputSchema
    assert spec.required_role == definition.required_role


async def test_bridged_tools_reuse_registered_mcp_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapters call the handler the MCP surface registered, with the run's operator."""
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def fake_get_tool(name: str) -> Any:
        entry = get_tool(name)
        assert entry is not None
        definition, _handler = entry

        async def _fake(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
            seen.append((name, operator.sub, arguments))
            return {"from": name}

        return definition, _fake

    monkeypatch.setattr(toolset_discovery, "get_tool", fake_get_tool)
    specs = {s.name: s for s in toolset_discovery.build_discovery_meta_tools()}
    operator = _make_operator(sub="op-distinct-principal")

    assert await specs["meho_connector_list"].handler(operator, {"status": "enabled"}) == {
        "from": "meho_connector_list"
    }
    assert await specs["list_targets"].handler(operator, {"limit": 10}) == {"from": "list_targets"}
    assert seen == [
        ("meho_connector_list", "op-distinct-principal", {"status": "enabled"}),
        ("list_targets", "op-distinct-principal", {"limit": 10}),
    ]


async def test_governed_handler_returns_jsonflux_handle_unchanged() -> None:
    """A reduced page with a result handle reaches the model as the handler built it."""
    entry = get_tool("list_targets")
    assert entry is not None
    reduced = {
        "row_count": 120,
        "next_cursor": "t-120",
        "handle": {"handle_id": "00000000-0000-0000-0000-000000000001"},
    }

    async def _handler(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        return reduced

    wrapped = toolset_discovery._governed_handler(entry[0], _handler)
    assert await wrapped(_make_operator(), {"limit": 120}) is reduced


async def test_governed_handler_enforces_mcp_input_schema() -> None:
    """Arguments outside the MCP schema never reach the handler (page-size bound)."""
    entry = get_tool("list_targets")
    assert entry is not None

    async def _never(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("handler must not run for schema-invalid arguments")

    wrapped = toolset_discovery._governed_handler(entry[0], _never)
    with pytest.raises(ModelRetry, match="invalid arguments"):
        await wrapped(_make_operator(), {"limit": 10_000})
    with pytest.raises(ModelRetry, match="invalid arguments"):
        await wrapped(_make_operator(), {"unexpected": True})


async def test_governed_handler_rechecks_role_at_call_time() -> None:
    """A model tool call under a role below the floor is refused before the handler."""
    entry = get_tool("result_query")
    assert entry is not None

    async def _never(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("handler must not run for a denied role")

    wrapped = toolset_discovery._governed_handler(entry[0], _never)
    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-read-only")
    with pytest.raises(ModelRetry, match="forbidden"):
        await wrapped(read_only, {"handle_id": "00000000-0000-0000-0000-000000000001"})


async def test_registered_tool_rechecks_role_against_call_operator() -> None:
    """A tool registered for one identity cannot run under a lower-role identity."""
    tools = resolve_agent_tools({"meta_tools": ["list_targets"]}, _make_operator())
    (tool,) = tools
    read_only = _make_operator(role=TenantRole.READ_ONLY, sub="op-read-only")
    with pytest.raises(ModelRetry, match="forbidden"):
        await tool.function(_make_run_context(read_only), limit=5)


async def test_result_query_handler_error_reaches_model_as_retry() -> None:
    """The real MCP handler's typed error becomes tool feedback, not a crash."""
    tools = resolve_agent_tools({"meta_tools": ["result_query"]}, _make_operator())
    (tool,) = tools
    with pytest.raises(ModelRetry, match="not a valid UUID"):
        await tool.function(_make_run_context(_make_operator()), handle_id="not-a-uuid")


async def test_preview_operation_respects_connector_allow_list() -> None:
    """``preview_operation`` cannot look at a connector the agent may not call."""
    spec = {"meta_tools": ["preview_operation"], "connectors": ["vault-1.x"]}
    tools = resolve_agent_tools(spec, _make_operator())
    (tool,) = tools
    with pytest.raises(ModelRetry, match="not in this agent's allowed connectors"):
        await tool.function(
            _make_run_context(_make_operator()),
            connector_id="vmware-rest-9.0",
            op_id="GET:/api/vcenter/cluster",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_context(operator: Operator) -> RunContext[Operator]:
    """Build a minimal RunContext carrying *operator* as deps.

    The wrapped tool only reads ``ctx.deps``; the rest of the RunContext is
    framework plumbing the adapter never touches, so a constructed context
    with the deps set is sufficient to exercise the wrapper directly without
    a full model run.
    """
    from pydantic_ai.usage import RunUsage

    return RunContext(deps=operator, model=_StubModel(), usage=RunUsage())


class _StubModel:
    """A stand-in model object for constructing a RunContext in unit tests.

    ``RunContext`` requires a ``model`` argument but the adapter never reads
    it; only ``system`` / ``model_name`` attributes are accessed by the
    framework's repr, so a tiny stub avoids constructing a real provider
    client.
    """

    system = "test"
    model_name = "stub"
