# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A child run gets at most what both its parent's and its own toolset allow.

* ``invoke_agent`` is on an agent only when its toolset's ``meta_tools`` list
  names it.
* :func:`~meho_backplane.agent.toolset.narrow_child_toolset` limits the child's
  toolset by the parent's: tools on both lists, connectors on both lists.
* A child of a parent with a connector limit cannot call a connector outside
  that limit, even when the child's own toolset has no limit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meho_backplane.agent import AgentDefinition, toolset
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agent.toolset import (
    narrow_child_toolset,
    resolve_agent_toolset,
    toolset_admits_invoke_agent,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.settings import get_settings

_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-agent",
        name="Agent Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_A,
        tenant_role=TenantRole.OPERATOR,
    )


def _request_parts(messages: list[ModelMessage]) -> list[Any]:
    return [part for m in messages if isinstance(m, ModelRequest) for part in m.parts]


# ---------------------------------------------------------------------------
# The toolset helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (None, False),
        ({}, False),
        ({"meta_tools": []}, False),
        ({"meta_tools": ["call_operation"]}, False),
        ({"meta_tools": ["invoke_agent"]}, True),
        ({"meta_tools": "invoke_agent"}, False),
    ],
)
def test_invoke_agent_only_when_the_list_names_it(
    spec: dict[str, Any] | None, expected: bool
) -> None:
    assert toolset_admits_invoke_agent(spec) is expected


def test_naming_invoke_agent_is_not_reported_missing() -> None:
    resolution = resolve_agent_toolset({"meta_tools": ["invoke_agent"]}, _operator())
    assert resolution.missing == ()
    assert resolution.tools == []


def test_child_tools_are_the_tools_on_both_lists() -> None:
    parent = {"meta_tools": ["invoke_agent", "call_operation", "search_operations"]}
    child = {"meta_tools": ["call_operation", "list_operation_groups", "invoke_agent"]}
    assert narrow_child_toolset(parent, child)["meta_tools"] == ["call_operation", "invoke_agent"]


def test_child_without_a_list_gets_the_default_tools_the_parent_names() -> None:
    parent = {"meta_tools": ["invoke_agent", "call_operation", "search_memory"]}
    # The child's default set has no opt-in tool and no invoke_agent.
    assert narrow_child_toolset(parent, {})["meta_tools"] == ["call_operation"]


def test_parent_with_empty_list_gives_the_child_no_tools() -> None:
    assert narrow_child_toolset({"meta_tools": []}, {})["meta_tools"] == []


@pytest.mark.parametrize(
    ("parent_connectors", "child_connectors", "expected"),
    [
        (None, None, None),
        (["a-1.x"], None, ["a-1.x"]),
        (None, ["b-1.x"], ["b-1.x"]),
        (["a-1.x", "b-1.x"], ["b-1.x", "c-1.x"], ["b-1.x"]),
        ([], ["b-1.x"], []),
    ],
)
def test_child_connectors_are_the_connectors_on_both_lists(
    parent_connectors: list[str] | None,
    child_connectors: list[str] | None,
    expected: list[str] | None,
) -> None:
    parent = {"meta_tools": ["invoke_agent"], "connectors": parent_connectors}
    child = {"connectors": child_connectors}
    assert narrow_child_toolset(parent, child)["connectors"] == expected


def test_child_keeps_its_other_keys() -> None:
    child = {"meta_tools": ["call_operation"], "note": "kept"}
    assert narrow_child_toolset({"meta_tools": ["call_operation"]}, child)["note"] == "kept"


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parent_toolset",
    [None, {}, {"meta_tools": []}, {"meta_tools": ["call_operation"]}],
)
@pytest.mark.asyncio
async def test_parent_without_invoke_agent_in_its_list_starts_no_child(
    parent_toolset: dict[str, Any] | None,
) -> None:
    resolved: list[str] = []
    seen_tools: list[list[str]] = []

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        resolved.append(agent_name)
        return AgentDefinition(name=agent_name, system_prompt="child", request_limit=3)

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_tools.append([tool.name for tool in info.function_tools])
        if any(isinstance(part, RetryPromptPart) for part in _request_parts(messages)):
            return ModelResponse(parts=[TextPart("parent done")])
        return ModelResponse(
            parts=[ToolCallPart("invoke_agent", {"agent_name": "wide", "inputs": "go"})]
        )

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(fn), child_agent_resolver=resolver
    )
    parent = AgentDefinition(
        name="narrow", system_prompt="parent", request_limit=5, toolset=parent_toolset
    )

    result = await runtime.result(runtime.start(parent, _operator(), "go"))

    assert result.output == "parent done"
    assert "invoke_agent" not in seen_tools[0]
    assert resolved == []


@pytest.mark.asyncio
async def test_child_cannot_call_a_connector_its_parent_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parent may use one connector; the wide child asks for another one."""
    dispatched: list[dict[str, Any]] = []

    async def _record(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(arguments)
        return {"status": "ok", "result": {}}

    spec = next(s for s in toolset._META_TOOL_CATALOG if s.name == "call_operation")
    monkeypatch.setattr(spec, "handler", _record)

    child_retries: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = _request_parts(messages)
        is_child = any(
            part.part_kind == "system-prompt" and "WIDE-CHILD" in part.content for part in parts
        )
        if is_child:
            retries = [part for part in parts if isinstance(part, RetryPromptPart)]
            if retries:
                child_retries.extend(str(part.content) for part in retries)
                return ModelResponse(parts=[TextPart("child stopped")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "call_operation",
                        {"connector_id": "refused-1.x", "op_id": "x.read", "params": {}},
                    )
                ]
            )
        if any(part.part_kind == "tool-return" for part in parts):
            return ModelResponse(parts=[TextPart("parent done")])
        return ModelResponse(
            parts=[ToolCallPart("invoke_agent", {"agent_name": "wide", "inputs": "go"})]
        )

    wide_child = AgentDefinition(
        name="wide",
        system_prompt="WIDE-CHILD",
        request_limit=5,
        toolset={"meta_tools": ["call_operation"]},  # no connector limit
    )

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return wide_child if agent_name == "wide" else None

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(fn), child_agent_resolver=resolver
    )
    parent = AgentDefinition(
        name="narrow",
        system_prompt="parent",
        request_limit=8,
        toolset={"meta_tools": ["invoke_agent", "call_operation"], "connectors": ["allowed-1.x"]},
    )

    result = await runtime.result(runtime.start(parent, _operator(), "go"))

    assert result.output == "parent done"
    assert dispatched == []
    assert any("not in this agent's allowed connectors" in text for text in child_retries)


@pytest.mark.asyncio
async def test_child_of_a_parent_with_only_invoke_agent_has_no_tools() -> None:
    child_tools: list[list[str]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = _request_parts(messages)
        if any(
            part.part_kind == "system-prompt" and "WIDE-CHILD" in part.content for part in parts
        ):
            child_tools.append([tool.name for tool in info.function_tools])
            return ModelResponse(parts=[TextPart("child done")])
        if any(part.part_kind == "tool-return" for part in parts):
            return ModelResponse(parts=[TextPart("parent done")])
        return ModelResponse(
            parts=[ToolCallPart("invoke_agent", {"agent_name": "wide", "inputs": "go"})]
        )

    wide_child = AgentDefinition(
        name="wide",
        system_prompt="WIDE-CHILD",
        request_limit=5,
        toolset={"meta_tools": ["call_operation", "list_operation_groups", "invoke_agent"]},
    )

    async def resolver(operator: Operator, agent_name: str) -> AgentDefinition | None:
        return wide_child

    runtime = PydanticAgentRun(
        model_factory=lambda: FunctionModel(fn), child_agent_resolver=resolver
    )
    parent = AgentDefinition(
        name="narrow",
        system_prompt="parent",
        request_limit=5,
        toolset={"meta_tools": ["invoke_agent"]},
    )

    result = await runtime.result(runtime.start(parent, _operator(), "go"))

    assert result.output == "parent done"
    # The child keeps invoke_agent (both lists name it) and nothing else.
    assert child_tools == [["invoke_agent"]]
