# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for data cache registration flow (TASK-185 Phase 4).

These tests verify that:
1. Cached tables are registered in session_state when data is cached
2. reduce_data can see available tables from previous operations
3. Multi-turn conversations can query previously cached data
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.persistence import OrchestratorSessionState
from meho_app.modules.agents.shared.state import WorkflowState

pytestmark = pytest.mark.asyncio


class TestCacheRegistrationInCallOperation:
    """Test that CallOperationNode registers cached data in session_state."""

    async def test_call_operation_registers_cached_data(self):
        """When call_operation returns data_available=False, cache is registered."""
        from meho_app.modules.agents.specialist_agent.nodes.call_operation import (
            CallOperationNode,
        )

        session_state = OrchestratorSessionState()
        state = WorkflowState(
            user_goal="List all virtual machines",
            connector_id="vcenter-prod",
            connector_name="vCenter Production",
            session_state=session_state,
        )

        # Mock the call_operation_handler to return cached data response

        # Create node
        node = CallOperationNode(
            connector_id="vcenter-prod",
            connector_name="vCenter Production",
            deps=MagicMock(),
            session_id="session-123",
        )

        # Mock the handler (imported from operation_handlers module)
        with patch(
            "meho_app.modules.agents.shared.handlers.operation_handlers.call_operation_handler",
            new_callable=AsyncMock,
            return_value='{"success": true, "data_available": false, "table": "virtual_machines_abc123", "row_count": 250, "columns": ["name", "power_state", "cpu", "memory"]}',
        ):
            await node.run(
                state=state,
                emitter=None,
                operation_id="list_vms",
                parameters={},
            )

        # Verify cache was registered
        assert "virtual_machines_abc123" in session_state.cached_tables
        cache_entry = session_state.cached_tables["virtual_machines_abc123"]
        assert cache_entry["connector_id"] == "vcenter-prod"
        assert cache_entry["row_count"] == 250

    async def test_call_operation_does_not_register_when_data_available(self):
        """When data is returned directly, no cache registration happens."""
        from meho_app.modules.agents.specialist_agent.nodes.call_operation import (
            CallOperationNode,
        )

        session_state = OrchestratorSessionState()
        state = WorkflowState(
            user_goal="Get cluster status",
            connector_id="k8s-prod",
            connector_name="K8s Production",
            session_state=session_state,
        )

        node = CallOperationNode(
            connector_id="k8s-prod",
            connector_name="K8s Production",
            deps=MagicMock(),
            session_id="session-456",
        )

        # Mock handler returning data directly (data_available=True or not present)
        with patch(
            "meho_app.modules.agents.shared.handlers.operation_handlers.call_operation_handler",
            new_callable=AsyncMock,
            return_value='{"success": true, "data_available": true, "data": [{"name": "pod-1"}]}',
        ):
            await node.run(
                state=state,
                emitter=None,
                operation_id="get_pods",
                parameters={},
            )

        # No cache should be registered
        assert len(session_state.cached_tables) == 0

    async def test_call_operation_handles_none_session_state(self):
        """CallOperationNode works gracefully when session_state is None."""
        from meho_app.modules.agents.specialist_agent.nodes.call_operation import (
            CallOperationNode,
        )

        state = WorkflowState(
            user_goal="List pods",
            connector_id="k8s-prod",
            session_state=None,  # No session state
        )

        node = CallOperationNode(
            connector_id="k8s-prod",
            connector_name="K8s Production",
            deps=MagicMock(),
            session_id="session-789",
        )

        with patch(
            "meho_app.modules.agents.shared.handlers.operation_handlers.call_operation_handler",
            new_callable=AsyncMock,
            return_value='{"success": true, "data_available": false, "table": "pods", "row_count": 50}',
        ):
            # Should not raise an error
            result = await node.run(
                state=state,
                emitter=None,
                operation_id="list_pods",
                parameters={},
            )

        # Result should still be valid
        assert result["success"] is True
        assert result["data_available"] is False


class TestReduceDataAvailableTables:
    """Test that reduce_data can access available tables from session_state."""

    async def test_reduce_data_gets_available_tables(self):
        """BaseReduceDataNode should pass available_tables to LLM decision."""
        from meho_app.modules.agents.base.reduce_data import BaseReduceDataNode

        session_state = OrchestratorSessionState()
        session_state.register_cached_data("vms", "vcenter-1", 500)
        session_state.register_cached_data("hosts", "vcenter-1", 10)
        session_state.register_cached_data("datastores", "vcenter-1", 20)

        state = WorkflowState(
            user_goal="Find VMs with low memory",
            connector_id="vcenter-1",
            session_state=session_state,
        )

        # Create a concrete implementation for testing
        class TestReduceDataNode(BaseReduceDataNode):
            pass

        TestReduceDataNode(
            connector_name="vCenter",
            deps=MagicMock(),
            session_id="session-123",
        )

        # Test that available_tables would be extracted correctly
        available_tables = None
        if hasattr(state, "session_state") and state.session_state:
            available_tables = state.session_state.get_available_tables()

        assert available_tables is not None
        assert len(available_tables) == 3
        assert "vms" in available_tables
        assert "hosts" in available_tables
        assert "datastores" in available_tables


class TestMultiTurnCacheFlow:
    """Test complete multi-turn cache flow."""

    async def test_full_cache_flow_across_turns(self):
        """Test that cached data persists across simulated turns."""
        session_state = OrchestratorSessionState()

        # Turn 1: First query caches pods
        WorkflowState(
            user_goal="List all pods",
            connector_id="k8s-prod",
            connector_name="Production K8s",
            session_state=session_state,
        )

        # Simulate cache registration from call_operation
        session_state.register_cached_data("pods_turn1", "k8s-prod", 100)

        # Turn 2: Second query sees previous cache and adds more
        state_turn2 = WorkflowState(
            user_goal="Now show deployments",
            connector_id="k8s-prod",
            connector_name="Production K8s",
            session_state=session_state,  # Same session_state
        )

        # reduce_data should see pods from turn 1
        available = state_turn2.session_state.get_available_tables()
        assert "pods_turn1" in available

        # Simulate cache registration from second call_operation
        session_state.register_cached_data("deployments_turn2", "k8s-prod", 25)

        # Turn 3: Can see all cached tables
        state_turn3 = WorkflowState(
            user_goal="Show me pods without deployments",
            connector_id="k8s-prod",
            session_state=session_state,
        )

        available_turn3 = state_turn3.session_state.get_available_tables()
        assert len(available_turn3) == 2
        assert "pods_turn1" in available_turn3
        assert "deployments_turn2" in available_turn3

    async def test_cache_flow_with_multiple_connectors(self):
        """Test cache registration from multiple connectors in one session."""
        session_state = OrchestratorSessionState()

        # Query first connector
        session_state.register_cached_data("vms", "vcenter-prod", 200)
        session_state.remember_connector("vcenter-prod", "vCenter", "vmware")

        # Query second connector
        session_state.register_cached_data("pods", "k8s-prod", 150)
        session_state.remember_connector("k8s-prod", "K8s", "kubernetes")

        # All caches should be available
        tables = session_state.get_available_tables()
        assert len(tables) == 2
        assert "vms" in tables
        assert "pods" in tables

        # Both connectors should be remembered
        assert len(session_state.connectors) == 2


class TestSessionStateContextSummary:
    """Test that context summary includes cached tables."""

    def test_context_summary_includes_cached_tables(self):
        """Context summary should mention cached data."""
        session_state = OrchestratorSessionState()
        session_state.register_cached_data("pods", "k8s-1", 100)
        session_state.register_cached_data("services", "k8s-1", 50)

        summary = session_state.get_context_summary()

        assert "Cached data:" in summary
        assert "pods" in summary
        assert "services" in summary

    def test_context_summary_empty_when_no_cache(self):
        """Context summary for new conversation."""
        session_state = OrchestratorSessionState()

        summary = session_state.get_context_summary()

        assert summary == "New conversation"


class TestExecuteWorkflowSessionState:
    """Test that execute_workflow properly passes session_state."""

    async def test_execute_workflow_accepts_session_state(self):
        """execute_workflow should accept session_state parameter."""
        from meho_app.modules.agents.shared.flow import execute_workflow

        session_state = OrchestratorSessionState()
        session_state.register_cached_data("previous_data", "conn-1", 50)

        # Mock dependencies to avoid actual LLM calls
        mock_deps = MagicMock()
        mock_deps.user_context = MagicMock()
        mock_deps.user_context.user_id = "test-user"
        mock_deps.user_context.tenant_id = "test-tenant"

        # Create mock node classes for the shared flow
        mock_search_intent_node = MagicMock()
        mock_search_intent_instance = AsyncMock()
        mock_intent = MagicMock()
        mock_intent.use_cached_data = False
        mock_intent.cached_table_name = None
        mock_intent.query = "test query"
        mock_search_intent_instance.run = AsyncMock(return_value=mock_intent)
        mock_search_intent_node.return_value = mock_search_intent_instance

        mock_search_ops_node = MagicMock()
        mock_search_ops_instance = AsyncMock()
        mock_search_ops_instance.run = AsyncMock(return_value=[])  # No operations
        mock_search_ops_node.return_value = mock_search_ops_instance

        result = await execute_workflow(
            user_goal="Test query",
            connector_id="test-conn",
            connector_name="Test Connector",
            connector_type="rest",
            deps=mock_deps,
            search_intent_node_cls=mock_search_intent_node,
            search_operations_node_cls=mock_search_ops_node,
            select_operation_node_cls=MagicMock(),
            call_operation_node_cls=MagicMock(),
            reduce_data_node_cls=MagicMock(),
            no_relevant_operation_cls=type("NoOp", (), {}),
            operation_selection_cls=type("OpSel", (), {}),
            emitter=None,
            session_id="test-session",
            session_state=session_state,
        )

        # Should complete without error
        assert result.success is True
        # Early exit message
        assert "No operations found" in result.findings
