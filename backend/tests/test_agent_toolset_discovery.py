# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for hosted connector discovery and backplane health (#4233).

A hosted agent must be able to find out what it may use without being told a
``connector_id`` in its prompt. These tests cover:

* ``meho_connector_list`` keeps only the rows in the definition's
  ``connectors`` allow-list, and keeps every row when there is no list. It
  drops the other rows before a long list goes behind a handle (#4121);
* the hosted ``meho_status`` tool returns the MCP health payload without the
  fields that only describe an MCP session, and needs the OPERATOR role;
* an agent run whose prompt names no connector lists the connectors, finds
  ``linux-ssh-1.x``, and calls ``linux.about``.

The connector query, the health probe, and the dispatcher are stubbed, so the
tests stay offline. The tool wrappers, the gates, and the agent loop are the
real code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage

import meho_backplane.api.v1.health as health_api
import meho_backplane.mcp.tools.connector_admin as connector_admin
import meho_backplane.operations.jsonflux_reducer as jsonflux_reducer_module
import meho_backplane.operations.meta_tools as meta_tools
from meho_backplane.agent import AgentDefinition, PydanticAgentRun
from meho_backplane.agent.toolset import resolve_agent_tools, resolve_agent_toolset
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.operations.ingest import ConnectorListItem
from meho_backplane.settings import get_settings

_TENANT = UUID("11111111-1111-1111-1111-111111111111")

_LINUX = "linux-ssh-1.x"
_VAULT = "vault-1.x"


def _row(connector_id: str) -> ConnectorListItem:
    product = connector_id.split("-", 1)[0]
    return ConnectorListItem(
        connector_id=connector_id,
        product=product,
        version="1.x",
        impl_id=connector_id.removesuffix("-1.x"),
        tenant_id=None,
        group_count=1,
        staged_group_count=0,
        enabled_group_count=1,
        disabled_group_count=0,
        operation_count=3,
        enabled_operation_count=3,
        dispatchable=True,
    )


class _MemoryValkey:
    """Minimal async Valkey shape the :class:`ResultHandleStore` reads and writes."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


class _Health:
    """Stands in for ``HealthResponse``; the handler only calls ``model_dump``."""

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        return {
            "operator": {"sub": "op-agent", "tenant_role": "operator"},
            "vault": {"reachable": True},
            "db": {"migrated": True},
            "sensor_runner": None,
            "pairings": [],
            "mcp_session_id_capture": "header",
            "mcp_protocol_version": "2025-06-18",
            "granted_scopes": ["mcp:admin"],
            "surface_summary": {"listed_working_tools": ["search_knowledge"]},
        }


class _StubModel:
    """``RunContext`` needs a model; the tool wrappers never read it."""

    system = "test"
    model_name = "stub"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def _two_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_list(*, operator: Operator, status: Any = None) -> list[ConnectorListItem]:
        return [_row(_LINUX), _row(_VAULT)]

    monkeypatch.setattr(connector_admin, "list_ingested_connectors", _fake_list)


def _operator(role: TenantRole = TenantRole.OPERATOR, tenant_id: UUID = _TENANT) -> Operator:
    return Operator(
        sub="op-agent",
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


def _ctx(operator: Operator) -> RunContext[Operator]:
    return RunContext(deps=operator, model=_StubModel(), usage=RunUsage())  # type: ignore[arg-type]


def _single_tool(spec: dict[str, Any]) -> Any:
    (tool,) = resolve_agent_tools(spec, _operator())
    return tool


@pytest.mark.usefixtures("_two_connectors")
async def test_connector_list_keeps_only_allowed_connectors() -> None:
    tool = _single_tool({"meta_tools": ["meho_connector_list"], "connectors": [_LINUX]})
    result = await tool.function(_ctx(_operator()))
    assert [row["connector_id"] for row in result["connectors"]] == [_LINUX]


@pytest.mark.usefixtures("_two_connectors")
async def test_connector_list_without_allow_list_keeps_every_row() -> None:
    tool = _single_tool({"meta_tools": ["meho_connector_list"]})
    result = await tool.function(_ctx(_operator()))
    assert [row["connector_id"] for row in result["connectors"]] == [_LINUX, _VAULT]


@pytest.mark.usefixtures("_two_connectors")
async def test_connector_list_with_empty_allow_list_shows_nothing() -> None:
    tool = _single_tool({"meta_tools": ["meho_connector_list"], "connectors": []})
    result = await tool.function(_ctx(_operator()))
    assert result["connectors"] == []


async def test_connector_list_filters_before_it_reduces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """60 connectors with one allowed come back inline: the filter runs first (#4121)."""
    many = [f"svc{i:02d}-1.x" for i in range(60)]

    async def _fake_list(*, operator: Operator, status: Any = None) -> list[ConnectorListItem]:
        return [_row(connector_id) for connector_id in many]

    monkeypatch.setattr(connector_admin, "list_ingested_connectors", _fake_list)
    tool = _single_tool({"meta_tools": ["meho_connector_list"], "connectors": [many[7]]})
    result = await tool.function(_ctx(_operator()))
    assert "handle" not in result
    assert [row["connector_id"] for row in result["connectors"]] == [many[7]]


async def test_connector_list_without_allow_list_reduces_a_long_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """60 connectors and no allow-list: the hosted tool returns a handle like MCP (#4121)."""
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(jsonflux_reducer_module, "get_result_handle_store", lambda: store)

    async def _fake_list(*, operator: Operator, status: Any = None) -> list[ConnectorListItem]:
        return [_row(f"svc{i:02d}-1.x") for i in range(60)]

    monkeypatch.setattr(connector_admin, "list_ingested_connectors", _fake_list)
    tool = _single_tool({"meta_tools": ["meho_connector_list"]})
    result = await tool.function(_ctx(_operator()))
    assert "connectors" not in result
    assert result["handle"]["total_rows"] == 60


async def test_status_drops_mcp_session_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def _fake_health(operator: Operator) -> _Health:
        seen.append(operator.sub)
        return _Health()

    monkeypatch.setattr(health_api, "build_health_response", _fake_health)
    tool = _single_tool({"meta_tools": ["meho_status"]})
    result = await tool.function(_ctx(_operator()))

    assert seen == ["op-agent"]
    assert set(result) == {"operator", "vault", "db", "sensor_runner", "pairings"}


def test_status_and_connector_list_are_in_the_default_surface() -> None:
    names = [t.name for t in resolve_agent_tools({}, _operator())]
    assert "meho_status" in names
    assert "meho_connector_list" in names


def test_status_needs_the_operator_role() -> None:
    read_only = _operator(TenantRole.READ_ONLY)
    resolution = resolve_agent_toolset({"meta_tools": ["meho_status"]}, read_only)
    assert resolution.tools == []
    assert resolution.disabled["meho_status"] == "role_below_floor"


async def test_status_refuses_arguments() -> None:
    tool = _single_tool({"meta_tools": ["meho_status"]})
    with pytest.raises(ModelRetry, match="takes no arguments"):
        await tool.function(_ctx(_operator()), verbose=True)


async def test_status_rechecks_role_at_call_time() -> None:
    tool = _single_tool({"meta_tools": ["meho_status"]})
    with pytest.raises(ModelRetry, match="forbidden"):
        await tool.function(_ctx(_operator(TenantRole.READ_ONLY)))


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _discover_then_call(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """A scripted model: list connectors, pick the Linux one, call ``linux.about``."""
    returns = _tool_returns(messages)
    if not returns:
        return ModelResponse(parts=[ToolCallPart(tool_name="meho_connector_list", args={})])
    last = returns[-1]
    if last.tool_name == "meho_connector_list":
        content = last.content if isinstance(last.content, dict) else json.loads(str(last.content))
        (connector_id,) = [
            row["connector_id"]
            for row in content["connectors"]
            if row["connector_id"].startswith("linux")
        ]
        call = {"connector_id": connector_id, "op_id": "linux.about", "params": {}}
        return ModelResponse(parts=[ToolCallPart(tool_name="call_operation", args=call)])
    return ModelResponse(parts=[TextPart("done")])


@pytest.mark.usefixtures("_two_connectors")
async def test_agent_without_connector_id_finds_linux_and_calls_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[dict[str, Any]] = []

    async def _fake_call(
        operator: Operator, arguments: dict[str, Any], *, approved: bool
    ) -> dict[str, Any]:
        dispatched.append(arguments)
        return {"status": "ok", "data": {"hostname": "example"}}

    monkeypatch.setattr(meta_tools, "_call_operation_impl", _fake_call)
    runtime = PydanticAgentRun(model_factory=lambda: FunctionModel(_discover_then_call))
    definition = AgentDefinition(
        name="discovery",
        system_prompt="Report what the Linux host says about itself.",
        request_limit=5,
        toolset={},
    )
    operator = _operator(tenant_id=uuid4())

    result = await runtime._build_agent(definition, operator).run("go", deps=operator)

    assert result.output == "done"
    assert dispatched == [{"connector_id": _LINUX, "op_id": "linux.about", "params": {}}]
