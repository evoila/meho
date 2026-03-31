# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for session_state integration in workflow states (Phase 4).

These tests verify that session_state is properly passed through the workflow
and that cache registration works correctly.
"""

from meho_app.modules.agents.persistence import OrchestratorSessionState
from meho_app.modules.agents.shared.state import WorkflowState


class TestWorkflowStateSessionIntegration:
    """Test session_state field in WorkflowState."""

    def test_workflow_state_accepts_session_state(self):
        """WorkflowState should accept optional session_state."""
        session_state = OrchestratorSessionState()

        state = WorkflowState(
            user_goal="List all pods",
            connector_id="conn-123",
            connector_name="K8s Prod",
            session_state=session_state,
        )

        assert state.session_state is session_state
        assert state.user_goal == "List all pods"
        assert state.connector_id == "conn-123"

    def test_workflow_state_session_state_defaults_to_none(self):
        """WorkflowState.session_state should default to None."""
        state = WorkflowState(
            user_goal="List all pods",
            connector_id="conn-123",
        )

        assert state.session_state is None

    def test_session_state_can_be_modified_via_workflow_state(self):
        """Changes to session_state should be reflected."""
        session_state = OrchestratorSessionState()

        state = WorkflowState(
            user_goal="List VMs",
            connector_id="vcenter-prod",
            connector_name="vCenter",
            session_state=session_state,
        )

        # Modify session_state via workflow state
        state.session_state.remember_connector(
            connector_id="vcenter-prod",
            connector_name="vCenter",
            connector_type="vmware",
            query="List VMs",
            status="success",
        )

        # Verify changes
        assert "vcenter-prod" in state.session_state.connectors
        assert state.session_state.primary_connector_id == "vcenter-prod"


class TestCacheRegistrationViaWorkflowState:
    """Test cache registration through workflow state."""

    def test_register_cached_data_via_session_state(self):
        """Cached data can be registered via session_state."""
        session_state = OrchestratorSessionState()

        state = WorkflowState(
            user_goal="Get all pods",
            connector_id="k8s-prod",
            session_state=session_state,
        )

        # Simulate what CallOperationNode does
        state.session_state.register_cached_data(
            table_name="pods_12345",
            connector_id="k8s-prod",
            row_count=150,
        )

        assert "pods_12345" in state.session_state.cached_tables
        assert state.session_state.cached_tables["pods_12345"]["connector_id"] == "k8s-prod"
        assert state.session_state.cached_tables["pods_12345"]["row_count"] == 150

    def test_get_available_tables_returns_cached_tables(self):
        """get_available_tables should return list of cached table names."""
        session_state = OrchestratorSessionState()
        session_state.register_cached_data("pods", "conn-1", 100)
        session_state.register_cached_data("services", "conn-1", 50)
        session_state.register_cached_data("deployments", "conn-1", 25)

        state = WorkflowState(
            user_goal="Analyze workloads",
            session_state=session_state,
        )

        tables = state.session_state.get_available_tables()

        assert len(tables) == 3
        assert "pods" in tables
        assert "services" in tables
        assert "deployments" in tables

    def test_cache_registration_with_none_session_state(self):
        """Workflow should handle None session_state gracefully."""
        state = WorkflowState(
            user_goal="List pods",
            connector_id="k8s-prod",
            session_state=None,
        )

        # This should not raise an error
        assert state.session_state is None

        # Code that checks session_state should handle None
        if state.session_state:
            state.session_state.register_cached_data("pods", "k8s-prod", 100)

        # No assertion needed - just verifying no exception


class TestSessionStateContextInReduceData:
    """Test that reduce_data can access session_state context."""

    def test_available_tables_accessible_via_state(self):
        """ReduceDataNode should be able to get available tables from state."""
        session_state = OrchestratorSessionState()
        session_state.register_cached_data("vms", "vcenter-1", 500)
        session_state.register_cached_data("hosts", "vcenter-1", 10)

        state = WorkflowState(
            user_goal="Find unhealthy VMs",
            connector_id="vcenter-1",
            session_state=session_state,
        )

        # Simulate what BaseReduceDataNode does
        available_tables = None
        if hasattr(state, "session_state") and state.session_state:
            available_tables = state.session_state.get_available_tables()

        assert available_tables is not None
        assert "vms" in available_tables
        assert "hosts" in available_tables

    def test_other_tables_can_be_identified_for_joins(self):
        """reduce_data should be able to identify other tables for JOINs."""
        session_state = OrchestratorSessionState()
        session_state.register_cached_data("pods", "k8s-1", 100)
        session_state.register_cached_data("nodes", "k8s-1", 5)
        session_state.register_cached_data("services", "k8s-1", 20)

        current_table = "pods"
        available_tables = session_state.get_available_tables()

        # Get other tables (excluding current)
        other_tables = [t for t in available_tables if t != current_table]

        assert len(other_tables) == 2
        assert current_table not in other_tables
        assert "nodes" in other_tables
        assert "services" in other_tables


class TestSessionStatePersistenceAcrossSteps:
    """Test that session_state persists across workflow steps."""

    def test_session_state_persists_across_step_executions(self):
        """session_state modifications should persist as workflow progresses."""
        session_state = OrchestratorSessionState()

        state = WorkflowState(
            user_goal="Analyze cluster health",
            connector_id="k8s-prod",
            connector_name="Production K8s",
            session_state=session_state,
        )

        # Step 1: SearchIntent (no state changes)
        state.steps_executed.append("search_intent: kubernetes cluster health")

        # Step 2: SearchOperations (no state changes)
        state.steps_executed.append("search_operations: found 5 operations")

        # Step 3: SelectOperation (no state changes)
        state.steps_executed.append("select_operation: get_pods")

        # Step 4: CallOperation (registers cached data)
        state.steps_executed.append("call_operation: get_pods")
        state.session_state.register_cached_data("pods_abc123", "k8s-prod", 250)

        # Verify state persists
        assert len(state.steps_executed) == 4
        assert "pods_abc123" in state.session_state.cached_tables
        assert state.session_state.cached_tables["pods_abc123"]["row_count"] == 250

    def test_multiple_cache_registrations_accumulate(self):
        """Multiple API calls should accumulate cached tables."""
        session_state = OrchestratorSessionState()

        state = WorkflowState(
            user_goal="Full cluster analysis",
            connector_id="k8s-prod",
            session_state=session_state,
        )

        # First API call
        state.session_state.register_cached_data("pods", "k8s-prod", 100)

        # Second API call
        state.session_state.register_cached_data("deployments", "k8s-prod", 25)

        # Third API call
        state.session_state.register_cached_data("services", "k8s-prod", 15)

        # All should be available
        tables = state.session_state.get_available_tables()
        assert len(tables) == 3
        assert set(tables) == {"pods", "deployments", "services"}
