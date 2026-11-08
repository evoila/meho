# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the hosted operation tools built from the MCP tools (#4754).

* ``test_hosted_schema_is_mcp_wire_schema_minus_left_out_fields`` — the
  parity check: each hosted schema equals the MCP wire schema minus the
  named fields in ``LEFT_OUT_FIELDS``.
* ``test_switch_off_keeps_the_hand_written_tools`` /
  ``test_switch_on_uses_the_mcp_tools`` — the setting picks the form; off is
  the default.
* ``test_hosted_run_gets_the_exact_op_contract`` — a hosted run calls
  ``search_operations`` with ``op_id`` and gets the contract back.
* ``test_call_operation_passes_preview_hash_to_the_dispatcher`` — the hosted
  ``call_operation`` passes ``preview_hash`` and ``idempotency_key`` on.
* The bad cases: an unknown ``op_id``, a search with neither ``query`` nor
  ``op_id``, and a left-out field sent anyway.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic_ai import ModelRetry, RunContext, ToolFailed
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

import meho_backplane.agent.toolset as toolset_mod
from meho_backplane.agent import AgentDefinition, PydanticAgentRun
from meho_backplane.agent.toolset import resolve_agent_tools
from meho_backplane.agent.toolset_operations import (
    LEFT_OUT_FIELDS,
    OPERATION_TOOL_NAMES,
    build_operation_meta_tools,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.mcp.registry import get_tool
from meho_backplane.operations import meta_tools
from meho_backplane.operations.meta_tools import UnknownOperationError
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")

_CONTRACT = {
    "operation": {
        "connector_id": "vault-1.x",
        "op_id": "vault.kv.read",
        "safety_level": "safe",
        "requires_approval": False,
        "parameter_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
}


def _operator() -> Operator:
    return Operator(
        sub="op-agent",
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )


def _unused_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("unused")])


def _ctx(operator: Operator) -> RunContext[Operator]:
    return RunContext(deps=operator, model=FunctionModel(_unused_model), usage=RunUsage())


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AGENT_OPERATION_TOOLS_FROM_MCP", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _specs() -> dict[str, Any]:
    return {spec.name: spec for spec in build_operation_meta_tools()}


@pytest.mark.parametrize("name", OPERATION_TOOL_NAMES)
def test_hosted_schema_is_mcp_wire_schema_minus_left_out_fields(name: str) -> None:
    entry = get_tool(name)
    assert entry is not None
    definition, _handler = entry
    wire = definition.to_wire()["inputSchema"]
    spec = _specs()[name]

    left_out = LEFT_OUT_FIELDS[name]
    # A stale name in the list would hide a real difference.
    assert set(left_out) <= set(wire["properties"])
    expected = {
        **wire,
        "properties": {k: v for k, v in wire["properties"].items() if k not in left_out},
        "required": [r for r in wire["required"] if r not in left_out],
    }
    assert spec.parameter_schema == expected
    assert spec.parameter_schema is not wire
    assert spec.required_role == definition.required_role
    assert spec.required_capability == definition.required_capability


def test_left_out_fields_cover_exactly_the_three_tools() -> None:
    assert set(LEFT_OUT_FIELDS) == set(OPERATION_TOOL_NAMES)
    assert LEFT_OUT_FIELDS["search_operations"] == ()
    assert LEFT_OUT_FIELDS["list_operation_groups"] == ()


def test_descriptions_follow_the_mcp_text() -> None:
    specs = _specs()
    for name in ("list_operation_groups", "search_operations"):
        entry = get_tool(name)
        assert entry is not None
        assert specs[name].description == entry[0].description
    assert "EXACT-OP MODE" in specs["search_operations"].description
    assert "unknown_op" in specs["search_operations"].description

    call_text = specs["call_operation"].description
    mcp_call = get_tool("call_operation")
    assert mcp_call is not None
    assert call_text.startswith(mcp_call[0].description.split(" For a long call", 1)[0])
    for field in ("async: true", "run_id", "wait_seconds"):
        assert field not in call_text
    assert "There is no `async` mode." in call_text


def test_switch_off_keeps_the_hand_written_tools() -> None:
    assert get_settings().agent_operation_tools_from_mcp is False
    tools = {t.name: t for t in resolve_agent_tools({}, _operator())}
    search = tools["search_operations"].tool_def.parameters_json_schema
    call = tools["call_operation"].tool_def.parameters_json_schema
    assert "op_id" not in search["properties"]
    assert "preview_hash" not in call["properties"]


@pytest.mark.usefixtures("switch_on")
def test_switch_on_uses_the_mcp_tools() -> None:
    tools = resolve_agent_tools({}, _operator())
    by_name = {t.name: t for t in tools}
    search = by_name["search_operations"].tool_def.parameters_json_schema
    call = by_name["call_operation"].tool_def.parameters_json_schema
    assert {"op_id", "include_scores"} <= set(search["properties"])
    assert {"preview_hash", "idempotency_key"} <= set(call["properties"])
    assert not set(LEFT_OUT_FIELDS["call_operation"]) & set(call["properties"])
    # Same names and order as the default catalog.
    assert [t.name for t in tools] == [
        s.name for s in toolset_mod._META_TOOL_CATALOG if s.name in by_name
    ]


@pytest.mark.usefixtures("switch_on")
async def test_hosted_run_gets_the_exact_op_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_search(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append(arguments)
        return _CONTRACT

    monkeypatch.setattr(meta_tools, "search_operations", fake_search)
    returned: list[Any] = []

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
        returns = [p for p in parts if isinstance(p, ToolReturnPart)]
        if returns:
            returned.extend(p.content for p in returns)
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "search_operations",
                    {"connector_id": "vault-1.x", "op_id": "vault.kv.read"},
                )
            ]
        )

    runtime = PydanticAgentRun(model_factory=lambda: FunctionModel(model_fn))
    definition = AgentDefinition(
        name="exact-op",
        system_prompt="find the contract",
        request_limit=3,
        toolset={"meta_tools": ["search_operations"]},
    )
    operator = _operator()
    result = await runtime._build_agent(definition, operator).run("go", deps=operator)

    assert result.output == "done"
    assert seen == [{"connector_id": "vault-1.x", "op_id": "vault.kv.read"}]
    assert returned == [_CONTRACT]


@pytest.mark.usefixtures("switch_on")
async def test_call_operation_passes_preview_hash_to_the_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatch_mock = AsyncMock(
        return_value=OperationResult(status="ok", op_id="vault.kv.destroy", duration_ms=1.0)
    )
    monkeypatch.setattr("meho_backplane.operations.meta_tools.dispatch", dispatch_mock)
    tools = {
        t.name: t for t in resolve_agent_tools({"meta_tools": ["call_operation"]}, _operator())
    }

    result = await tools["call_operation"].function(
        _ctx(_operator()),
        connector_id="vault-1.x",
        op_id="vault.kv.destroy",
        target=None,
        params={"path": "secret/app"},
        preview_hash="<preview-hash>",
        idempotency_key="<retry-key>",
    )

    assert result["status"] == "ok"
    kwargs = dispatch_mock.await_args.kwargs
    assert kwargs["preview_hash"] == "<preview-hash>"
    assert kwargs["idempotency_key"] == "<retry-key>"
    assert kwargs["params"] == {"path": "secret/app"}


@pytest.mark.usefixtures("switch_on")
async def test_call_operation_refuses_a_left_out_field(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch_mock = AsyncMock()
    monkeypatch.setattr("meho_backplane.operations.meta_tools.dispatch", dispatch_mock)
    tools = {
        t.name: t for t in resolve_agent_tools({"meta_tools": ["call_operation"]}, _operator())
    }

    with pytest.raises(ModelRetry, match="invalid arguments"):
        await tools["call_operation"].function(
            _ctx(_operator()), connector_id="vault-1.x", op_id="vault.kv.read", work_ref="gh:x#1"
        )
    dispatch_mock.assert_not_awaited()


async def test_search_without_query_or_op_id_is_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock()
    monkeypatch.setattr(meta_tools, "search_operations", fake)

    with pytest.raises(ModelRetry, match="invalid arguments"):
        await _specs()["search_operations"].handler(_operator(), {"connector_id": "vault-1.x"})
    fake.assert_not_awaited()


async def test_unknown_op_is_a_failed_tool_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        raise UnknownOperationError(
            "no enabled operation 'vault.nope'", connector_id="vault-1.x", op_id="vault.nope"
        )

    monkeypatch.setattr(meta_tools, "search_operations", fake_search)

    with pytest.raises(ToolFailed, match="reason: unknown_op") as excinfo:
        await _specs()["search_operations"].handler(
            _operator(), {"connector_id": "vault-1.x", "op_id": "vault.nope"}
        )
    assert "free-text `query`" in str(excinfo.value)
