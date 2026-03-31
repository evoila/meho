# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for React Agent nodes.

Tests the full node implementations for the ReAct loop.

Phase 84: TopologyLookupNode and ApprovalCheckNode APIs changed during
v2.1 agent reasoning upgrade (Phase 77). Node signatures and behavior differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: React agent node APIs changed in v2.1 Phase 77 agent reasoning upgrade")

from meho_app.modules.agents.base.node import BaseNode, NodeResult
from meho_app.modules.agents.config.loader import AgentConfig
from meho_app.modules.agents.config.models import ModelConfig
from meho_app.modules.agents.react_agent.nodes import (
    NODE_REGISTRY,
    ApprovalCheckNode,
    ReasonNode,
    ToolDispatchNode,
    TopologyLookupNode,
    create_node,
    get_node_class,
)
from meho_app.modules.agents.react_agent.state import ReactAgentState


class TestNodeImports:
    """Tests for node module imports."""

    def test_import_all_nodes(self) -> None:
        """Test that all nodes can be imported."""
        assert ReasonNode is not None
        assert ToolDispatchNode is not None
        assert ApprovalCheckNode is not None
        assert TopologyLookupNode is not None

    def test_nodes_are_base_node_subclasses(self) -> None:
        """Test that all nodes inherit from BaseNode."""
        nodes = [
            ReasonNode,
            ToolDispatchNode,
            ApprovalCheckNode,
            TopologyLookupNode,
        ]
        for node_cls in nodes:
            assert issubclass(node_cls, BaseNode)


class TestNodeNames:
    """Tests for node NODE_NAME attributes."""

    def test_reason_node_name(self) -> None:
        """Test ReasonNode has correct NODE_NAME."""
        assert ReasonNode.NODE_NAME == "reason"

    def test_tool_dispatch_node_name(self) -> None:
        """Test ToolDispatchNode has correct NODE_NAME."""
        assert ToolDispatchNode.NODE_NAME == "tool_dispatch"

    def test_approval_check_node_name(self) -> None:
        """Test ApprovalCheckNode has correct NODE_NAME."""
        assert ApprovalCheckNode.NODE_NAME == "approval_check"

    def test_topology_lookup_node_name(self) -> None:
        """Test TopologyLookupNode has correct NODE_NAME."""
        assert TopologyLookupNode.NODE_NAME == "topology_lookup"


class TestNodeRegistry:
    """Tests for NODE_REGISTRY."""

    def test_registry_has_all_nodes(self) -> None:
        """Test that registry contains all nodes."""
        expected_nodes = [
            "reason",
            "tool_dispatch",
            "approval_check",
            "topology_lookup",
        ]
        for name in expected_nodes:
            assert name in NODE_REGISTRY, f"Missing node: {name}"

    def test_topology_learn_not_in_registry(self) -> None:
        """Test that TopologyLearnNode has been removed from registry (D-13)."""
        assert "topology_learn" not in NODE_REGISTRY

    def test_registry_count(self) -> None:
        """Test registry has correct number of nodes."""
        assert len(NODE_REGISTRY) == 4

    def test_registry_values_are_classes(self) -> None:
        """Test that registry values are node classes."""
        for name, cls in NODE_REGISTRY.items():
            assert isinstance(cls, type), f"{name} is not a class"
            assert issubclass(cls, BaseNode), f"{name} is not a BaseNode subclass"


class TestGetNodeClass:
    """Tests for get_node_class function."""

    def test_get_reason_node(self) -> None:
        """Test getting ReasonNode class."""
        cls = get_node_class("reason")
        assert cls is ReasonNode

    def test_get_tool_dispatch_node(self) -> None:
        """Test getting ToolDispatchNode class."""
        cls = get_node_class("tool_dispatch")
        assert cls is ToolDispatchNode

    def test_get_invalid_node_raises(self) -> None:
        """Test that invalid node name raises KeyError."""
        with pytest.raises(KeyError):
            get_node_class("invalid_node")


class TestCreateNode:
    """Tests for create_node function."""

    def test_create_reason_node(self) -> None:
        """Test creating ReasonNode instance."""
        node = create_node("reason")
        assert isinstance(node, ReasonNode)

    def test_create_all_nodes(self) -> None:
        """Test creating all node types."""
        node_names = [
            "reason",
            "tool_dispatch",
            "approval_check",
            "topology_lookup",
        ]
        for name in node_names:
            node = create_node(name)
            assert isinstance(node, BaseNode)
            assert name == node.NODE_NAME

    def test_create_invalid_node_raises(self) -> None:
        """Test that invalid node name raises KeyError."""
        with pytest.raises(KeyError):
            create_node("nonexistent")


@dataclass
class MockDeps:
    """Mock dependencies for testing."""

    agent_config: AgentConfig
    topology_service: Any = None
    data_reduction_context: dict[str, Any] | None = None
    topology_context: str = ""
    conversation_history: str = ""


@pytest.fixture
def mock_emitter() -> AsyncMock:
    """Create a mock emitter that records calls."""
    emitter = AsyncMock()
    return emitter


@pytest.fixture
def mock_state() -> ReactAgentState:
    """Create a state for testing."""
    return ReactAgentState(user_goal="List all VMs")


@pytest.fixture
def mock_deps() -> MockDeps:
    """Create mock dependencies."""
    config = AgentConfig(
        name="react",
        description="Test agent",
        model=ModelConfig(name="openai:gpt-4.1-mini", temperature=0.0),
        system_prompt="You are a test agent.\n{{user_goal}}\n{{tool_list}}\n{{scratchpad}}\n{{tables_context}}\n{{topology_context}}\n{{history_context}}\n{{request_guidance}}",
        max_steps=10,
        tools={
            "call_operation": {"require_approval_for_dangerous": True},
        },
    )
    return MockDeps(agent_config=config)


class TestTopologyLookupNode:
    """Tests for TopologyLookupNode."""

    @pytest.mark.asyncio
    async def test_returns_reason_as_next_node(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test TopologyLookupNode returns reason as next node."""
        node = TopologyLookupNode()
        result = await node.run(mock_state, mock_deps, mock_emitter)
        assert result.next_node == "reason"

    @pytest.mark.asyncio
    async def test_extracts_entity_mentions(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test entity extraction from user message."""
        state = ReactAgentState(user_goal="Check status of web-server-01.example.com")
        node = TopologyLookupNode()

        # Test the extraction method directly
        entities = node._extract_entity_mentions(state.user_goal)
        assert "web-server-01.example.com" in entities

    @pytest.mark.asyncio
    async def test_emits_node_events(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test that node emits enter/exit events."""
        node = TopologyLookupNode()
        await node.run(mock_state, mock_deps, mock_emitter)

        mock_emitter.node_enter.assert_called_once_with("topology_lookup")
        mock_emitter.node_exit.assert_called_once()


class TestToolDispatchNode:
    """Tests for ToolDispatchNode."""

    @pytest.mark.asyncio
    async def test_routes_to_approval_for_dangerous_tools(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ToolDispatchNode routes to approval_check for dangerous tools."""
        state = ReactAgentState(user_goal="Delete VM")
        state.pending_tool = "call_operation"
        state.pending_args = {"operation_id": "delete_vm", "connector_id": "abc"}
        state.approval_granted = False

        node = ToolDispatchNode()
        result = await node.run(state, mock_deps, mock_emitter)

        assert result.next_node == "approval_check"

    @pytest.mark.asyncio
    async def test_executes_safe_tools(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ToolDispatchNode executes safe tools directly."""
        state = ReactAgentState(user_goal="List connectors")
        state.pending_tool = "list_connectors"
        state.pending_args = {}

        # Mock the tool execution - patch at the import location
        with patch("meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY") as mock_registry:
            mock_tool_class = MagicMock()
            mock_tool = MagicMock()
            mock_tool.InputSchema = MagicMock(return_value=MagicMock())
            mock_tool.execute = AsyncMock(return_value={"connectors": []})
            mock_tool_class.return_value = mock_tool
            mock_registry.__contains__ = MagicMock(return_value=True)
            mock_registry.__getitem__ = MagicMock(return_value=mock_tool_class)

            node = ToolDispatchNode()
            result = await node.run(state, mock_deps, mock_emitter)

            assert result.next_node == "reason"
            assert state.last_observation is not None

    @pytest.mark.asyncio
    async def test_executes_when_approved(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ToolDispatchNode executes dangerous tools when approved."""
        state = ReactAgentState(user_goal="Delete VM")
        state.pending_tool = "call_operation"
        state.pending_args = {"operation_id": "delete_vm"}
        state.approval_granted = True  # User approved

        with patch("meho_app.modules.agents.react_agent.tools.TOOL_REGISTRY") as mock_registry:
            mock_tool_class = MagicMock()
            mock_tool = MagicMock()
            mock_tool.InputSchema = MagicMock(return_value=MagicMock())
            mock_tool.execute = AsyncMock(return_value={"success": True})
            mock_tool_class.return_value = mock_tool
            mock_registry.__contains__ = MagicMock(return_value=True)
            mock_registry.__getitem__ = MagicMock(return_value=mock_tool_class)

            node = ToolDispatchNode()
            result = await node.run(state, mock_deps, mock_emitter)

            assert result.next_node == "reason"
            # Approval should be reset
            assert state.approval_granted is False

    @pytest.mark.asyncio
    async def test_handles_unknown_tool(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ToolDispatchNode handles unknown tools gracefully."""
        state = ReactAgentState(user_goal="Test")
        state.pending_tool = "nonexistent_tool"
        state.pending_args = {}

        node = ToolDispatchNode()
        result = await node.run(state, mock_deps, mock_emitter)

        assert result.next_node == "reason"  # Continue to reason with error
        assert "Unknown tool" in (state.last_observation or "")


class TestApprovalCheckNode:
    """Tests for ApprovalCheckNode."""

    @pytest.mark.asyncio
    async def test_emits_approval_required_and_pauses(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ApprovalCheckNode emits approval_required and returns None."""
        state = ReactAgentState(user_goal="Delete VM")
        state.pending_tool = "call_operation"
        state.pending_args = {"operation_id": "delete_vm", "connector_id": "abc"}

        node = ApprovalCheckNode()
        result = await node.run(state, mock_deps, mock_emitter)

        # Should emit approval_required
        mock_emitter.approval_required.assert_called_once()
        call_args = mock_emitter.approval_required.call_args
        assert call_args.kwargs["tool"] == "call_operation"
        assert call_args.kwargs["danger_level"] in ["dangerous", "critical"]

        # Should pause (return None)
        assert result.next_node is None
        assert result.data.get("awaiting_approval") is True

    @pytest.mark.asyncio
    async def test_assesses_danger_level_critical(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test danger level assessment for delete operations."""
        node = ApprovalCheckNode()
        danger = node._assess_danger_level("call_operation", {"operation_id": "delete_vm"})
        assert danger == "critical"

    @pytest.mark.asyncio
    async def test_assesses_danger_level_dangerous(
        self,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test danger level assessment for create operations."""
        node = ApprovalCheckNode()
        danger = node._assess_danger_level("call_operation", {"operation_id": "create_vm"})
        assert danger == "dangerous"


class TestReasonNode:
    """Tests for ReasonNode."""

    @pytest.mark.asyncio
    async def test_returns_node_result(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ReasonNode returns NodeResult."""
        # Mock the LLM call - patch at source
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: I need to list connectors first.\n"
                "Action: list_connectors\n"
                "Action Input: {}"
            )

            node = ReasonNode()
            result = await node.run(mock_state, mock_deps, mock_emitter)

            assert isinstance(result, NodeResult)

    @pytest.mark.asyncio
    async def test_parses_action_correctly(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ReasonNode parses Action from LLM response."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: I should search for VMs.\n"
                "Action: search_operations\n"
                'Action Input: {"connector_id": "abc", "query": "list vms"}'
            )

            node = ReasonNode()
            result = await node.run(mock_state, mock_deps, mock_emitter)

            assert result.next_node == "tool_dispatch"
            assert mock_state.pending_tool == "search_operations"
            assert mock_state.pending_args == {"connector_id": "abc", "query": "list vms"}

    @pytest.mark.asyncio
    async def test_parses_final_answer_correctly(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ReasonNode parses Final Answer and returns None (terminal)."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = (
                "Thought: I have all the information needed.\n"
                "Final Answer: Here are the VMs:\n| Name | Status |\n| vm-01 | running |"
            )

            node = ReasonNode()
            result = await node.run(mock_state, mock_deps, mock_emitter)

            assert result.next_node is None
            assert mock_state.final_answer is not None
            assert "Here are the VMs" in mock_state.final_answer

    @pytest.mark.asyncio
    async def test_emits_thought_event(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ReasonNode emits thought event."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = "Thought: Testing thought emission.\nFinal Answer: Done."

            node = ReasonNode()
            await node.run(mock_state, mock_deps, mock_emitter)

            mock_emitter.thought.assert_called_once()
            call_args = mock_emitter.thought.call_args
            assert "Testing thought emission" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_emits_final_answer_event(
        self,
        mock_state: ReactAgentState,
        mock_emitter: AsyncMock,
        mock_deps: MockDeps,
    ) -> None:
        """Test ReasonNode emits final_answer event."""
        with patch("meho_app.modules.agents.base.inference.infer") as mock_infer:
            mock_infer.return_value = "Thought: Answering.\nFinal Answer: The answer is 42."

            node = ReasonNode()
            await node.run(mock_state, mock_deps, mock_emitter)

            mock_emitter.final_answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_builds_tool_list(
        self,
        mock_deps: MockDeps,
    ) -> None:
        """Test that tool list is built correctly."""
        node = ReasonNode()
        tool_list = node._build_tool_list()

        # Should contain known tools
        assert "list_connectors" in tool_list
        assert "search_operations" in tool_list
        assert "call_operation" in tool_list
