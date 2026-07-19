# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the MCP agent-invocation tools (G11.1-T4 / #811).

Covers ``meho.agents.run`` + ``meho.agents.run_status`` over the MCP
Streamable-HTTP transport (the same dispatch the REST routes use, modulo
the ``method="MCP"`` distinction):

* ``meho.agents.run`` (sync) returns the final output; the same run is
  poll-able via ``meho.agents.run_status``.
* A missing name surfaces as 'agent_not_found'; a disabled definition as
  'agent_disabled'.
* Both tools require the ``operator`` role (read_only is filtered out of
  ``tools/list`` and rejected at call time).

A deterministic :class:`~meho_backplane.agent.run.AgentRun` is injected via
:func:`~meho_backplane.agent.invocation.reset_agent_invoker_for_testing`
so no real LLM is hit.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from meho_backplane.agent.invocation import AgentInvoker, reset_agent_invoker_for_testing
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, AgentRun, Tenant
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


@pytest.fixture(autouse=True)
def _reset_invoker() -> Iterator[None]:
    """Clear the injected invoker after each test so it doesn't leak."""
    yield
    reset_agent_invoker_for_testing(None)


def _final_text(text: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _install_invoker(text: str = "final answer") -> None:
    reset_agent_invoker_for_testing(
        AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _final_text(text)))
    )


async def _seed_definition(
    *,
    tenant_id: UUID,
    name: str = "triage",
    enabled: bool = True,
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        # The tenant row is seeded by the seeded_operator_tenant fixture.
        if await session.get(Tenant, tenant_id) is None:
            session.add(Tenant(id=tenant_id, slug="t", name="T"))
        session.add(
            AgentDefinition(
                tenant_id=tenant_id,
                name=name,
                identity_ref=f"agent:{name}",
                model_tier="standard",
                system_prompt="You triage incidents.",
                toolset={},
                turn_budget=5,
                output_schema=None,
                enabled=enabled,
                created_by_sub="seed-admin",
            )
        )
        await session.commit()


def _result_dict(response: Any) -> dict[str, Any]:
    body = response.json()
    assert "error" not in body, body
    content = body["result"]["content"]
    return json.loads(content[0]["text"])


def _call(client: TestClient, name: str, arguments: dict[str, Any], rpc_id: int = 1) -> Any:
    return post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_operator_sees_run_tools(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An ``operator`` sees both invocation tools in tools/list."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "meho.agents.run" in names
    assert "meho.agents.run_status" in names
    assert "meho.agents.list_runs" in names


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_run_then_poll_round_trip(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """meho.agents.run returns the output; meho.agents.run_status polls it."""
    client, op = client_with_operator
    await _seed_definition(tenant_id=op.tenant_id)
    _install_invoker("triaged via mcp")

    run = _call(client, "meho.agents.run", {"name": "triage", "input": "go"})
    body = _result_dict(run)
    assert body["status"] == "succeeded"
    assert body["output"] == {"text": "triaged via mcp"}
    handle = body["run_id"]

    status = _call(client, "meho.agents.run_status", {"handle": handle}, rpc_id=2)
    status_body = _result_dict(status)
    assert status_body["run_id"] == handle
    assert status_body["status"] == "succeeded"
    assert status_body["output"] == {"text": "triaged via mcp"}
    # The run is traceable to its agent on the status face (#2472).
    assert status_body["agent_name"] == "triage"
    assert status_body["agent_definition_id"] is not None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_returns_tenant_runs(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """meho.agents.list_runs returns the operator's tenant's runs (#1662)."""
    client, op = client_with_operator
    await _seed_definition(tenant_id=op.tenant_id)
    _install_invoker("triaged via mcp")

    run = _call(client, "meho.agents.run", {"name": "triage", "input": "go"})
    handle = _result_dict(run)["run_id"]

    listed = _call(client, "meho.agents.list_runs", {}, rpc_id=2)
    body = _result_dict(listed)
    assert [r["run_id"] for r in body["runs"]] == [handle]
    # work_ref is surfaced on the list row (None here -- no header bound).
    assert body["runs"][0]["work_ref"] is None
    assert body["runs"][0]["status"] == "succeeded"
    # The run is traceable to its agent on the list face (#2472).
    assert body["runs"][0]["agent_name"] == "triage"
    assert body["runs"][0]["agent_definition_id"] is not None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_filters_by_agent_name(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """agent_name narrows to that agent; an unknown name yields [] (#2472)."""
    client, op = client_with_operator
    await _seed_definition(tenant_id=op.tenant_id, name="triage")
    await _seed_definition(tenant_id=op.tenant_id, name="planner")
    _install_invoker("done")

    triage_run = _call(client, "meho.agents.run", {"name": "triage", "input": "go"})
    triage_handle = _result_dict(triage_run)["run_id"]
    _call(client, "meho.agents.run", {"name": "planner", "input": "go"}, rpc_id=2)

    only_triage = _result_dict(
        _call(client, "meho.agents.list_runs", {"agent_name": "triage"}, rpc_id=3)
    )
    assert [r["run_id"] for r in only_triage["runs"]] == [triage_handle]
    assert only_triage["runs"][0]["agent_name"] == "triage"

    # An unknown name is not an error (-32602); it is an empty list.
    unknown = _result_dict(
        _call(client, "meho.agents.list_runs", {"agent_name": "no-such-agent"}, rpc_id=4)
    )
    assert unknown["runs"] == []


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_list_runs_null_agent_name_for_dangling_definition(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A run whose definition was deleted lists with agent_name null, no 500 (#2472)."""
    client, op = client_with_operator
    await _seed_definition(tenant_id=op.tenant_id, name="triage")
    _install_invoker("done")

    run = _call(client, "meho.agents.run", {"name": "triage", "input": "go"})
    handle = _result_dict(run)["run_id"]

    # Delete the definition after the run -- the run row's agent_definition_id
    # is a soft-FK, so it now dangles.
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        definition = await session.get(
            AgentDefinition,
            (await session.get(AgentRun, UUID(handle))).agent_definition_id,
        )
        await session.delete(definition)
        await session.commit()

    listed = _result_dict(_call(client, "meho.agents.list_runs", {}, rpc_id=2))
    assert [r["run_id"] for r in listed["runs"]] == [handle]
    assert listed["runs"][0]["agent_name"] is None
    assert listed["runs"][0]["agent_definition_id"] is not None

    status = _result_dict(_call(client, "meho.agents.run_status", {"handle": handle}, rpc_id=3))
    assert status["agent_name"] is None


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_run_unknown_agent_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Running an absent agent surfaces as 'agent_not_found'."""
    client, _op = client_with_operator
    _install_invoker()
    resp = _call(client, "meho.agents.run", {"name": "nope", "input": "go"})
    body = resp.json()
    assert "error" in body
    assert body["error"]["message"] == "agent_not_found"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_run_disabled_agent_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Running a disabled agent surfaces as 'agent_disabled'."""
    client, op = client_with_operator
    await _seed_definition(tenant_id=op.tenant_id, enabled=False)
    _install_invoker()
    resp = _call(client, "meho.agents.run", {"name": "triage", "input": "go"})
    body = resp.json()
    assert "error" in body
    assert body["error"]["message"] == "agent_disabled"


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_run_status_unknown_handle_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Polling an unknown run handle surfaces as 'agent_run_not_found'."""
    client, _op = client_with_operator
    _install_invoker()
    resp = _call(
        client,
        "meho.agents.run_status",
        {"handle": "00000000-0000-0000-0000-000000000000"},
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["message"] == "agent_run_not_found"


# ---------------------------------------------------------------------------
# G11.5-T6 #1080 -- pre-execution budget gate contract on the MCP boundary
# ---------------------------------------------------------------------------
#
# The MCP transport has no spec-blessed "too many requests" code, so
# ``meho.agents.run`` maps :class:`BudgetExceededError` onto the JSON-RPC
# ``-32602`` (invalid-params) message that mirrors the way the REST 429
# carries its structured detail body -- the message starts with
# ``"budget_exceeded: "`` so a client parser distinguishes the budget
# refusal from agent_not_found / agent_disabled (same -32602 surface,
# different prefix). The global kill switch is the simplest deterministic
# trigger; no DB seeding required.


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
@pytest.mark.asyncio
async def test_mcp_run_returns_invalid_params_when_budget_exceeded_pre_execution(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """meho.agents.run on a budget-refused principal raises -32602 with ``budget_exceeded:``.

    Contract (G11.5-T6 #1080): the MCP tool catches
    :class:`BudgetExceededError` from
    :meth:`AgentInvoker.run` and re-raises
    :class:`McpInvalidParamsError` whose message starts with
    ``"budget_exceeded: "`` plus the gate's reason. The dispatcher
    serialises that into the JSON-RPC ``error.code = -32602`` envelope.
    """
    from meho_backplane.settings import get_settings

    client, op = client_with_operator
    await _seed_definition(tenant_id=op.tenant_id)
    _install_invoker()
    monkeypatch.setenv("AGENT_RUNS_DISABLED_GLOBAL", "true")
    get_settings.cache_clear()

    resp = _call(client, "meho.agents.run", {"name": "triage", "input": "go"})
    body = resp.json()
    assert "error" in body, body
    # Invalid-params on the JSON-RPC envelope.
    assert body["error"]["code"] == -32602, body
    # Prefix discriminates the budget refusal from sibling refusals
    # (agent_not_found / agent_disabled) that ride the same code.
    assert body["error"]["message"].startswith("budget_exceeded: "), body
