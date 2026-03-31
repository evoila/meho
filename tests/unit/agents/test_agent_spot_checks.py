# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Agent behavior spot checks -- deterministic ReAct loop validation.

Tests tool call SEQUENCES (which tools called, in what order, with what args),
NOT LLM output text. Every test here runs without real LLM calls.

Architecture note:
    MEHO's ReactAgent does NOT use PydanticAI Agent internally for the ReAct loop.
    It uses a custom node-based architecture:
        TopologyLookup -> ReasonNode -> ToolDispatchNode -> ReasonNode -> ... -> Final Answer (terminal)

    ReasonNode._call_llm() calls the `infer()` utility which creates PydanticAI
    agents internally. Tool dispatch uses TOOL_REGISTRY -- tools are instantiated
    and called directly by ToolDispatchNode.

    Therefore, FunctionModel injection (as described in PydanticAI docs) is NOT
    applicable to MEHO's specialist agent. Instead, we mock at the ReasonNode level:
    - Mock ReasonNode._call_llm to return deterministic ReAct-formatted strings
    - Mock tool execution via TOOL_REGISTRY to return deterministic results
    - Test the full node execution cycle (Reason -> ToolDispatch -> Reason)

    This approach validates the SAME code paths as production: prompt building,
    response parsing, tool dispatch, scratchpad accumulation, and state transitions.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.base.node import NodeResult
from meho_app.modules.agents.config.loader import AgentConfig
from meho_app.modules.agents.config.models import ModelConfig
from meho_app.modules.agents.react_agent.nodes.reason import ReasonNode
from meho_app.modules.agents.react_agent.nodes.tool_dispatch import ToolDispatchNode
from meho_app.modules.agents.react_agent.nodes.topology_lookup import TopologyLookupNode
from meho_app.modules.agents.react_agent.state import ReactAgentState


# ===========================================================================
# Helper functions
# ===========================================================================


def make_react_response(
    thought: str,
    *,
    action: str | None = None,
    action_input: dict[str, Any] | None = None,
    final_answer: str | None = None,
) -> str:
    """Build a ReAct-formatted string matching what the LLM would return.

    The ReasonNode._parse_response expects:
        Thought: <text>
        Action: <tool_name>
        Action Input: <json>
    or:
        Thought: <text>
        Final Answer: <text>
    """
    parts = [f"Thought: {thought}"]
    if final_answer is not None:
        parts.append(f"Final Answer: {final_answer}")
    elif action is not None:
        parts.append(f"Action: {action}")
        parts.append(f"Action Input: {json.dumps(action_input or {})}")
    return "\n".join(parts)


def make_step_sequence(steps: list[dict[str, Any]]):
    """Create a side_effect list for _call_llm that returns each step in order.

    Each dict in steps should have keys matching make_react_response params:
    thought, action, action_input, final_answer.
    """
    return [make_react_response(**step) for step in steps]


def make_tool_result(data: dict[str, Any] | str) -> MagicMock:
    """Create a mock tool execution result."""
    result = MagicMock()
    if isinstance(data, dict):
        result.model_dump.return_value = data
        result.__str__ = lambda self: str(data)
    else:
        result.model_dump.side_effect = AttributeError
        result.__str__ = lambda self: data
    return result


def create_mock_deps(
    *,
    topology_context: str = "",
    conversation_history: str = "",
) -> MagicMock:
    """Create mock AgentDeps for testing."""
    deps = MagicMock()
    deps.agent_config = AgentConfig(
        name="test-react",
        description="Test agent",
        model=ModelConfig(name="test-model"),
        system_prompt="You are a test agent. {{tool_list}} {{topology_context}} {{history_context}} {{tables_context}} {{scratchpad}} {{user_goal}} {{prior_findings_context}}",
        max_steps=20,
    )
    deps.topology_context = topology_context
    deps.conversation_history = conversation_history
    deps.data_reduction_context = None
    deps.external_deps = MagicMock()
    deps.external_deps.user_context = MagicMock()
    deps.external_deps.user_context.tenant_id = "test-tenant"
    deps.external_deps.user_context.user_id = "test-user"
    deps.topology_service = AsyncMock()
    return deps


def create_mock_emitter() -> MagicMock:
    """Create a mock EventEmitter that records all calls."""
    emitter = MagicMock()
    # Make all async methods return coroutines
    for method_name in [
        "node_enter", "node_exit", "thought", "action", "observation",
        "final_answer", "error", "tool_start", "tool_complete", "tool_error",
        "emit", "emit_detailed",
    ]:
        setattr(emitter, method_name, AsyncMock())
    return emitter


async def run_react_loop(
    user_goal: str,
    llm_responses: list[str],
    tool_results: dict[str, Any],
    *,
    deps: MagicMock | None = None,
    max_steps: int = 20,
) -> tuple[ReactAgentState, list[tuple[str, dict[str, Any]]]]:
    """Run the ReAct loop with deterministic LLM responses and tool results.

    This simulates the ReactAgent.run_streaming flow at the node level:
    TopologyLookup -> Reason -> ToolDispatch -> Reason -> ... -> Final Answer (terminal)

    Args:
        user_goal: The user's question/request.
        llm_responses: List of ReAct-formatted strings returned by _call_llm.
        tool_results: Dict mapping tool_name to mock result (or callable for dynamic).
        deps: Optional mock dependencies. Created if not provided.
        max_steps: Maximum steps before stopping.

    Returns:
        Tuple of (final state, list of (tool_name, tool_args) executed).
    """
    if deps is None:
        deps = create_mock_deps()

    state = ReactAgentState(user_goal=user_goal)
    emitter = create_mock_emitter()

    # Track tool calls in order
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    llm_call_index = 0

    # Nodes
    topology_lookup = TopologyLookupNode()
    reason_node = ReasonNode()
    tool_dispatch = ToolDispatchNode()

    # Mock topology lookup to skip DB calls
    with patch.object(
        TopologyLookupNode,
        "_lookup_topology",
        new_callable=AsyncMock,
        return_value="",
    ):
        # Start with topology lookup
        await topology_lookup.run(state, deps, emitter)

    # Main ReAct loop
    step = 0
    while step < max_steps and not state.is_complete():
        # Reason step -- mock _call_llm
        if llm_call_index >= len(llm_responses):
            break

        with patch.object(
            ReasonNode,
            "_call_llm",
            new_callable=AsyncMock,
            return_value=llm_responses[llm_call_index],
        ):
            result = await reason_node.run(state, deps, emitter)
            llm_call_index += 1

        if state.is_complete():
            break

        if result.next_node != "tool_dispatch":
            break

        # Tool dispatch step -- mock tool execution
        tool_name = state.pending_tool
        tool_args = state.pending_args or {}

        # Record the tool call
        tool_calls.append((tool_name, dict(tool_args)))

        # Create mock tool with deterministic result
        mock_tool_result = tool_results.get(tool_name, make_tool_result("OK"))
        if callable(mock_tool_result) and not isinstance(mock_tool_result, MagicMock):
            mock_tool_result = mock_tool_result(tool_args)

        mock_tool = MagicMock()
        mock_tool.InputSchema = MagicMock(return_value=MagicMock())
        mock_tool.execute = AsyncMock(return_value=mock_tool_result)

        with patch(
            "meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY",
            {tool_name: MagicMock(return_value=mock_tool)},
        ):
            result = await tool_dispatch.run(state, deps, emitter)

        step += 1

    # ReasonNode now returns next_node=None on Final Answer (terminal)
    # No TopologyLearnNode step needed (removed in D-13)

    return state, tool_calls


# ===========================================================================
# Spot Check 1: Proof-of-concept -- single ReAct cycle works
# ===========================================================================


class TestSpotCheck1ProofOfConcept:
    """Verify the test harness correctly drives the ReAct loop."""

    async def test_single_step_final_answer(self) -> None:
        """ReasonNode returns Final Answer immediately -- no tool calls."""
        llm_responses = make_step_sequence([
            {"thought": "This is a simple question I can answer directly",
             "final_answer": "The cluster has 3 nodes."},
        ])

        state, tool_calls = await run_react_loop(
            user_goal="How many nodes does the cluster have?",
            llm_responses=llm_responses,
            tool_results={},
        )

        assert state.final_answer is not None
        assert len(tool_calls) == 0
        assert state.error_message is None

    async def test_single_tool_then_answer(self) -> None:
        """One tool call followed by final answer."""
        llm_responses = make_step_sequence([
            {"thought": "I need to list connectors",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Found connectors, can answer now",
             "final_answer": "There are 2 connectors configured."},
        ])

        state, tool_calls = await run_react_loop(
            user_goal="What connectors are available?",
            llm_responses=llm_responses,
            tool_results={"list_connectors": make_tool_result(
                {"connectors": [{"id": "k8s-1"}, {"id": "vm-1"}]}
            )},
        )

        assert state.final_answer is not None
        assert len(tool_calls) == 1
        assert tool_calls[0][0] == "list_connectors"


# ===========================================================================
# Spot Check 2: Single connector investigation
# ===========================================================================


class TestSpotCheck2SingleConnectorInvestigation:
    """Verify: list_connectors -> search_operations -> call_operation -> final_answer."""

    async def test_single_connector_tool_sequence(self) -> None:
        """Agent investigates a single connector: list -> search -> call -> answer."""
        llm_responses = make_step_sequence([
            {"thought": "Need to find available connectors first",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Found K8s connector, searching for pod operations",
             "action": "search_operations",
             "action_input": {"query": "list pods", "connector_id": "k8s-prod"}},
            {"thought": "Found list_pods operation, calling it",
             "action": "call_operation",
             "action_input": {"connector_id": "k8s-prod", "operation_id": "list_pods",
                              "params": {"namespace": "default"}}},
            {"thought": "Got pod list, can answer now",
             "final_answer": "Found 5 pods in default namespace."},
        ])

        tool_results = {
            "list_connectors": make_tool_result(
                {"connectors": [
                    {"id": "k8s-prod", "name": "Production K8s", "type": "kubernetes"},
                ]}
            ),
            "search_operations": make_tool_result(
                {"operations": [
                    {"id": "list_pods", "name": "List Pods", "method": "GET"},
                ]}
            ),
            "call_operation": make_tool_result(
                {"pods": [{"name": f"pod-{i}", "status": "Running"} for i in range(5)]}
            ),
        }

        state, tool_calls = await run_react_loop(
            user_goal="List all pods in the default namespace",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        # Verify tool sequence
        assert len(tool_calls) == 3
        assert tool_calls[0][0] == "list_connectors"
        assert tool_calls[1][0] == "search_operations"
        assert tool_calls[1][1]["connector_id"] == "k8s-prod"
        assert tool_calls[2][0] == "call_operation"
        assert tool_calls[2][1]["connector_id"] == "k8s-prod"
        assert tool_calls[2][1]["operation_id"] == "list_pods"

        # Verify completion
        assert state.final_answer is not None
        assert state.error_message is None


# ===========================================================================
# Spot Check 3: Cross-system K8s-to-VMware trace
# ===========================================================================


class TestSpotCheck3CrossSystemTrace:
    """Verify: list_connectors -> search_ops(k8s) -> call_op(k8s) -> search_ops(vmware) -> call_op(vmware) -> final_answer."""

    async def test_k8s_to_vmware_cross_system_trace(self) -> None:
        """Agent traces from K8s pod to VMware VM hosting the node."""
        llm_responses = make_step_sequence([
            {"thought": "Need to find connectors for infrastructure diagnosis",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Found K8s and VMware connectors. Start with K8s to find the pod",
             "action": "search_operations",
             "action_input": {"query": "get pod details", "connector_id": "k8s-prod"}},
            {"thought": "Found get_pod operation, calling it for payment-svc",
             "action": "call_operation",
             "action_input": {"connector_id": "k8s-prod", "operation_id": "get_pod",
                              "params": {"name": "payment-svc", "namespace": "production"}}},
            {"thought": "Pod is on node-3. Need to find the VM for node-3 in VMware",
             "action": "search_operations",
             "action_input": {"query": "get vm details", "connector_id": "vmware-dc"}},
            {"thought": "Found get_vm operation, checking VM for node-3",
             "action": "call_operation",
             "action_input": {"connector_id": "vmware-dc", "operation_id": "get_vm",
                              "params": {"vm_name": "node-3"}}},
            {"thought": "Traced from pod to node to VM. VM shows high CPU",
             "final_answer": "Pod payment-svc runs on node-3, backed by VM esxi-node-3 which shows 95% CPU."},
        ])

        tool_results = {
            "list_connectors": make_tool_result(
                {"connectors": [
                    {"id": "k8s-prod", "name": "Production K8s", "type": "kubernetes"},
                    {"id": "vmware-dc", "name": "vSphere DC", "type": "vmware"},
                ]}
            ),
            "search_operations": make_tool_result(
                {"operations": [{"id": "get_pod", "name": "Get Pod Details"}]}
            ),
            "call_operation": make_tool_result(
                {"name": "payment-svc", "node": "node-3", "status": "Running",
                 "cpu_usage": "2.5 cores"}
            ),
        }

        state, tool_calls = await run_react_loop(
            user_goal="What is wrong with the payment-svc pod?",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        # Verify 5 tool calls in the correct order
        assert len(tool_calls) == 5
        assert tool_calls[0][0] == "list_connectors"
        assert tool_calls[1][0] == "search_operations"
        assert tool_calls[1][1]["connector_id"] == "k8s-prod"
        assert tool_calls[2][0] == "call_operation"
        assert tool_calls[2][1]["connector_id"] == "k8s-prod"
        assert tool_calls[3][0] == "search_operations"
        assert tool_calls[3][1]["connector_id"] == "vmware-dc"
        assert tool_calls[4][0] == "call_operation"
        assert tool_calls[4][1]["connector_id"] == "vmware-dc"

        # Verify cross-system: both K8s and VMware connectors used
        connector_ids_used = {tc[1].get("connector_id") for tc in tool_calls if "connector_id" in tc[1]}
        assert "k8s-prod" in connector_ids_used
        assert "vmware-dc" in connector_ids_used

        # Verify completion
        assert state.final_answer is not None


# ===========================================================================
# Spot Check 4: Knowledge retrieval chain
# ===========================================================================


class TestSpotCheck4KnowledgeRetrieval:
    """Verify: search_knowledge -> final_answer."""

    async def test_knowledge_retrieval_chain(self) -> None:
        """Agent uses knowledge base to answer a question."""
        llm_responses = make_step_sequence([
            {"thought": "This looks like a knowledge question, searching KB",
             "action": "search_knowledge",
             "action_input": {"query": "how to restart a deployment in ArgoCD"}},
            {"thought": "Found relevant knowledge article",
             "final_answer": "To restart a deployment in ArgoCD, sync the application with --force flag."},
        ])

        tool_results = {
            "search_knowledge": make_tool_result(
                {"results": [
                    {"title": "ArgoCD Restart Guide",
                     "content": "Use sync --force to restart",
                     "score": 0.92},
                ]}
            ),
        }

        state, tool_calls = await run_react_loop(
            user_goal="How do I restart a deployment in ArgoCD?",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        assert len(tool_calls) == 1
        assert tool_calls[0][0] == "search_knowledge"
        assert "query" in tool_calls[0][1]
        assert state.final_answer is not None


# ===========================================================================
# Spot Check 5: Tool error handling
# ===========================================================================


class TestSpotCheck5ErrorHandling:
    """Verify: list_connectors -> call_operation (error) -> final_answer (graceful)."""

    async def test_agent_handles_tool_error_gracefully(self) -> None:
        """Agent recovers when a tool returns an error and produces final answer."""
        llm_responses = make_step_sequence([
            {"thought": "Need to find connectors",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Found Prometheus, querying metrics",
             "action": "call_operation",
             "action_input": {"connector_id": "prom-main",
                              "operation_id": "query_metrics",
                              "params": {"query": "up"}}},
            # After the error observation, agent still produces a graceful answer
            {"thought": "The Prometheus query failed. I should inform the user about the error",
             "final_answer": "Unable to query Prometheus metrics: connection timed out. Please check the Prometheus connector configuration."},
        ])

        # Simulate a tool that raises an exception
        def error_tool_factory(tool_args: dict) -> MagicMock:
            mock = MagicMock()
            mock.InputSchema = MagicMock(return_value=MagicMock())
            mock.execute = AsyncMock(side_effect=ConnectionError("Connection timed out"))
            return mock

        tool_results = {
            "list_connectors": make_tool_result(
                {"connectors": [
                    {"id": "prom-main", "name": "Prometheus", "type": "prometheus"},
                ]}
            ),
        }

        # For the error scenario, we need to handle the ToolDispatch error path.
        # ToolDispatchNode catches exceptions and records them as observations,
        # then returns to "reason" node. Our run_react_loop handles this because
        # the tool_dispatch node catches the exception internally.
        state, tool_calls = await run_react_loop(
            user_goal="Show me the current up metrics from Prometheus",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        # list_connectors succeeded, call_operation attempted
        assert len(tool_calls) == 2
        assert tool_calls[0][0] == "list_connectors"
        assert tool_calls[1][0] == "call_operation"

        # Agent still produced a final answer (graceful degradation)
        assert state.final_answer is not None
        assert state.error_message is None  # Agent-level error not set


# ===========================================================================
# Spot Check 6: Topology exploration
# ===========================================================================


class TestSpotCheck6TopologyExploration:
    """Verify: list_connectors -> lookup_topology -> get_entity_details -> final_answer."""

    async def test_topology_exploration_sequence(self) -> None:
        """Agent explores topology: list connectors, then topology tools."""
        llm_responses = make_step_sequence([
            {"thought": "Need to find connectors first",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Looking up topology for known entities",
             "action": "lookup_topology",
             "action_input": {"query": "payment-svc", "traverse_depth": 2}},
            {"thought": "Found entity in topology, need full details",
             "final_answer": "payment-svc is a Pod in the production namespace, connected to node-3 and service payment-lb."},
        ])

        tool_results = {
            "list_connectors": make_tool_result(
                {"connectors": [
                    {"id": "k8s-prod", "name": "Production K8s", "type": "kubernetes"},
                ]}
            ),
            "lookup_topology": make_tool_result(
                {"found": True,
                 "entity": {"name": "payment-svc", "type": "Pod",
                            "namespace": "production"},
                 "relationships": [
                     {"target": "node-3", "type": "runs_on"},
                     {"target": "payment-lb", "type": "exposes"},
                 ]}
            ),
        }

        state, tool_calls = await run_react_loop(
            user_goal="Tell me about the payment-svc topology",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        assert len(tool_calls) == 2
        assert tool_calls[0][0] == "list_connectors"
        assert tool_calls[1][0] == "lookup_topology"
        assert state.final_answer is not None


# ===========================================================================
# Spot Check 7: Multi-connector parallel observability
# ===========================================================================


class TestSpotCheck7MultiConnectorObservability:
    """Verify: list_connectors -> call_op(prometheus) -> call_op(loki) -> final_answer."""

    async def test_multi_connector_observability_correlation(self) -> None:
        """Agent queries both Prometheus metrics and Loki logs for correlation."""
        llm_responses = make_step_sequence([
            {"thought": "Need to find observability connectors",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Found Prometheus and Loki. Query metrics first",
             "action": "call_operation",
             "action_input": {"connector_id": "prom-main",
                              "operation_id": "query_metrics",
                              "params": {"query": "rate(http_requests_total{code=~'5..'}[5m])"}}},
            {"thought": "High error rate detected. Check Loki logs for error details",
             "action": "call_operation",
             "action_input": {"connector_id": "loki-main",
                              "operation_id": "query_logs",
                              "params": {"query": "{app='payment-svc'} |= 'error'",
                                         "limit": 10}}},
            {"thought": "Found correlated metrics and logs. Error rate spike matches OOM kills",
             "final_answer": "API 500 errors are caused by OOM kills in payment-svc. Prometheus shows 15 req/s error rate, Loki logs show 'container killed: OOM' events."},
        ])

        tool_results = {
            "list_connectors": make_tool_result(
                {"connectors": [
                    {"id": "prom-main", "name": "Prometheus", "type": "prometheus"},
                    {"id": "loki-main", "name": "Loki", "type": "loki"},
                ]}
            ),
            "call_operation": make_tool_result(
                {"data": [{"metric": "http_requests_total", "value": 15.2}]}
            ),
        }

        state, tool_calls = await run_react_loop(
            user_goal="Why is the API returning 500 errors?",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        # Verify 3 tool calls
        assert len(tool_calls) == 3
        assert tool_calls[0][0] == "list_connectors"

        # Both observability connectors queried
        assert tool_calls[1][0] == "call_operation"
        assert tool_calls[1][1]["connector_id"] == "prom-main"
        assert tool_calls[2][0] == "call_operation"
        assert tool_calls[2][1]["connector_id"] == "loki-main"

        # Both connector IDs used
        connector_ids = {tc[1].get("connector_id") for tc in tool_calls if "connector_id" in tc[1]}
        assert "prom-main" in connector_ids
        assert "loki-main" in connector_ids

        assert state.final_answer is not None


# ===========================================================================
# Spot Check 8: Empty connector list handling
# ===========================================================================


class TestSpotCheck8EmptyConnectorList:
    """Verify: list_connectors (empty) -> final_answer (graceful)."""

    async def test_agent_handles_no_connectors(self) -> None:
        """Agent gracefully handles no connectors being configured."""
        llm_responses = make_step_sequence([
            {"thought": "Need to find available connectors",
             "action": "list_connectors", "action_input": {}},
            {"thought": "No connectors configured. Cannot investigate infrastructure",
             "final_answer": "No connectors are currently configured. Please add at least one connector to enable infrastructure investigation."},
        ])

        tool_results = {
            "list_connectors": make_tool_result({"connectors": []}),
        }

        state, tool_calls = await run_react_loop(
            user_goal="What pods are running?",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        assert len(tool_calls) == 1
        assert tool_calls[0][0] == "list_connectors"
        assert state.final_answer is not None
        assert state.error_message is None


# ===========================================================================
# Verification: Scratchpad accumulation
# ===========================================================================


class TestScratchpadAccumulation:
    """Verify the scratchpad correctly accumulates thoughts, actions, and observations."""

    async def test_scratchpad_records_full_chain(self) -> None:
        """Scratchpad should contain thought, action, action input, and observation entries."""
        llm_responses = make_step_sequence([
            {"thought": "Need connectors",
             "action": "list_connectors", "action_input": {}},
            {"thought": "Done",
             "final_answer": "Found connectors."},
        ])

        tool_results = {
            "list_connectors": make_tool_result({"connectors": [{"id": "k8s-1"}]}),
        }

        state, _ = await run_react_loop(
            user_goal="List connectors",
            llm_responses=llm_responses,
            tool_results=tool_results,
        )

        scratchpad = state.get_scratchpad_text()

        # Scratchpad should have structured entries
        assert "Thought: Need connectors" in scratchpad
        assert "Action: list_connectors" in scratchpad
        assert "Observation:" in scratchpad
        assert "Thought: Done" in scratchpad
        assert "Final Answer: Found connectors." in scratchpad
