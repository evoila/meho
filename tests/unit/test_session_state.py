# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Comprehensive unit tests for agent session state management.

Tests all aspects of state tracking including:
- Connector context management
- Workflow tracking
- Error learning
- Cross-system correlation
- State serialization
"""

from datetime import UTC, datetime, timedelta

import pytest

from meho_app.modules.agents.session_state import (
    AgentSessionState,
    ConnectorContext,
    OperationType,
    WorkflowStep,
)

# ============================================================================
# CONNECTOR CONTEXT TESTS
# ============================================================================


@pytest.mark.unit
def test_connector_context_creation():
    """Test creating a connector context"""
    ctx = ConnectorContext(
        connector_id="conn-123",
        connector_name="vCenter",
        connector_type="vcenter",
        last_used=datetime.now(tz=UTC),
    )

    assert ctx.connector_id == "conn-123"
    assert ctx.connector_name == "vCenter"
    assert ctx.connector_type == "vcenter"
    assert len(ctx.known_endpoints) == 0
    assert len(ctx.recent_data) == 0
    assert len(ctx.failed_queries) == 0


@pytest.mark.unit
def test_connector_context_add_endpoint():
    """Test adding endpoints to connector context"""
    ctx = ConnectorContext(
        connector_id="conn-123",
        connector_name="vCenter",
        connector_type="vcenter",
        last_used=datetime.now(tz=UTC),
    )

    ctx.add_endpoint("/api/vcenter/vm", "endpoint-uuid-1", "GET")
    ctx.add_endpoint("/api/vcenter/host", "endpoint-uuid-2", "GET")

    assert len(ctx.known_endpoints) == 2
    assert ctx.get_endpoint("/api/vcenter/vm", "GET") == "endpoint-uuid-1"
    assert ctx.get_endpoint("/api/vcenter/host", "GET") == "endpoint-uuid-2"
    assert ctx.get_endpoint("/api/nonexistent", "GET") is None


@pytest.mark.unit
def test_connector_context_store_data():
    """Test storing API response data"""
    ctx = ConnectorContext(
        connector_id="conn-123",
        connector_name="vCenter",
        connector_type="vcenter",
        last_used=datetime.now(tz=UTC),
    )

    vms_data = [{"id": "vm-1", "name": "Web-01"}]
    ctx.store_data("vms", vms_data)

    # Should retrieve immediately
    retrieved = ctx.get_data("vms")
    assert retrieved == vms_data

    # Should return None for non-existent data
    assert ctx.get_data("nonexistent") is None


@pytest.mark.unit
def test_connector_context_data_expiration():
    """Test that cached data expires"""
    ctx = ConnectorContext(
        connector_id="conn-123",
        connector_name="vCenter",
        connector_type="vcenter",
        last_used=datetime.now(tz=UTC) - timedelta(hours=2),
    )

    # Store data 2 hours ago
    ctx.recent_data["old_data"] = {
        "data": {"test": "value"},
        "retrieved_at": datetime.now(tz=UTC) - timedelta(hours=2),
    }

    # Should be expired (max_age=3600 seconds = 1 hour)
    assert ctx.get_data("old_data", max_age_seconds=3600) is None

    # Should still exist with longer max_age
    assert ctx.get_data("old_data", max_age_seconds=10000) is not None


@pytest.mark.unit
def test_connector_context_record_failure():
    """Test recording failed queries"""
    ctx = ConnectorContext(
        connector_id="conn-123",
        connector_name="vCenter",
        connector_type="vcenter",
        last_used=datetime.now(tz=UTC),
    )

    ctx.record_failure("reset VM endpoint")
    ctx.record_failure("power off VM endpoint")

    assert len(ctx.failed_queries) == 2
    assert "reset VM endpoint" in ctx.failed_queries
    assert "power off VM endpoint" in ctx.failed_queries


# ============================================================================
# WORKFLOW STEP TESTS
# ============================================================================


@pytest.mark.unit
def test_workflow_step_creation():
    """Test creating a workflow step"""
    step = WorkflowStep(
        step_id="step-1",
        description="Get VM list",
        status="pending",
        tool_name="call_operation",
        tool_args={"connector_id": "conn-123", "operation_id": "op-456"},
    )

    assert step.step_id == "step-1"
    assert step.description == "Get VM list"
    assert step.status == "pending"
    assert step.tool_name == "call_operation"
    assert step.result is None
    assert step.error is None


# ============================================================================
# AGENT SESSION STATE TESTS
# ============================================================================


@pytest.mark.unit
def test_session_state_creation():
    """Test creating a session state"""
    state = AgentSessionState()

    assert len(state.connectors) == 0
    assert state.primary_connector_id is None
    assert state.operation_type is None
    assert state.operation_goal is None


@pytest.mark.unit
def test_session_state_get_or_create_connector():
    """Test getting or creating connector context"""
    state = AgentSessionState()

    # Create new connector
    ctx1 = state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    assert ctx1.connector_id == "conn-123"
    assert ctx1.connector_name == "vCenter"
    assert len(state.connectors) == 1

    # Get existing connector
    ctx2 = state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    assert ctx1 is ctx2  # Should be same instance
    assert len(state.connectors) == 1  # No duplicate

    # Create different connector
    ctx3 = state.get_or_create_connector("conn-456", "Kubernetes", "kubernetes")
    assert len(state.connectors) == 2
    assert ctx3.connector_id == "conn-456"


@pytest.mark.unit
def test_session_state_get_active_connector():
    """Test getting active connector"""
    state = AgentSessionState()

    # No active connector initially
    assert state.get_active_connector() is None

    # Create connector and set as primary
    state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    state.primary_connector_id = "conn-123"

    active = state.get_active_connector()
    assert active is not None
    assert active.connector_id == "conn-123"


@pytest.mark.unit
def test_session_state_switch_connector():
    """Test switching between connectors"""
    state = AgentSessionState()

    # Create two connectors
    state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    state.get_or_create_connector("conn-456", "Kubernetes", "kubernetes")

    # Set first as primary
    state.primary_connector_id = "conn-123"
    assert state.get_active_connector().connector_id == "conn-123"

    # Switch to second
    state.switch_connector("conn-456")
    assert state.primary_connector_id == "conn-456"
    assert state.get_active_connector().connector_id == "conn-456"


@pytest.mark.unit
def test_session_state_set_operation():
    """Test setting operation context"""
    state = AgentSessionState()

    state.set_operation(OperationType.DIAGNOSIS, "Diagnose app-prod performance")

    assert state.operation_type == OperationType.DIAGNOSIS
    assert state.operation_goal == "Diagnose app-prod performance"


@pytest.mark.unit
def test_session_state_add_workflow_step():
    """Test adding workflow steps"""
    state = AgentSessionState()

    step1 = WorkflowStep(
        step_id="step-1",
        description="Get VM list",
        status="completed",
        tool_name="call_operation",
        tool_args={},
    )

    step2 = WorkflowStep(
        step_id="step-2",
        description="Analyze VMs",
        status="in_progress",
        tool_name="interpret_results",
        tool_args={},
    )

    state.add_workflow_step(step1)
    state.add_workflow_step(step2)

    assert len(state.workflow_steps) == 2
    assert state.workflow_steps[0].step_id == "step-1"
    assert state.workflow_steps[1].step_id == "step-2"


@pytest.mark.unit
def test_session_state_get_workflow_progress():
    """Test getting workflow progress summary"""
    state = AgentSessionState()

    # No workflow initially
    progress = state.get_workflow_progress()
    assert progress["total"] == 0
    assert progress["completed"] == 0

    # Add steps with various statuses
    state.workflow_steps = [
        WorkflowStep("s1", "Step 1", "completed", "tool1", {}),
        WorkflowStep("s2", "Step 2", "completed", "tool2", {}),
        WorkflowStep("s3", "Step 3", "in_progress", "tool3", {}),
        WorkflowStep("s4", "Step 4", "failed", "tool4", {}),
        WorkflowStep("s5", "Step 5", "pending", "tool5", {}),
    ]

    progress = state.get_workflow_progress()
    assert progress["total"] == 5
    assert progress["completed"] == 2
    assert progress["in_progress"] == 1
    assert progress["failed"] == 1


@pytest.mark.unit
def test_session_state_learn_preference():
    """Test learning user preferences"""
    state = AgentSessionState()

    state.learn_preference("format", "table")
    state.learn_preference("show_details", True)
    state.learn_preference("max_results", 50)

    assert state.get_preference("format") == "table"
    assert state.get_preference("show_details") is True
    assert state.get_preference("max_results") == 50
    assert state.get_preference("nonexistent", "default") == "default"


@pytest.mark.unit
def test_session_state_add_documentation_reference():
    """Test adding documentation references"""
    state = AgentSessionState()

    state.add_documentation_reference("doc://api/reset-vm")
    state.add_documentation_reference("doc://troubleshooting/vm", "VM troubleshooting guide")

    assert len(state.referenced_docs) == 2
    assert "doc://api/reset-vm" in state.referenced_docs
    assert state.learned_facts["doc://troubleshooting/vm"] == "VM troubleshooting guide"


@pytest.mark.unit
def test_session_state_record_error():
    """Test recording errors"""
    state = AgentSessionState()

    state.record_error(
        tool_name="search_operations",
        error_msg="No operation found",
        context={"connector_id": "conn-123", "query": "reset VM"},
    )

    assert len(state.recent_errors) == 1
    assert state.recent_errors[0]["tool"] == "search_operations"
    assert state.recent_errors[0]["error"] == "No operation found"


@pytest.mark.unit
def test_session_state_has_similar_error():
    """Test checking for similar errors"""
    state = AgentSessionState()

    # Record error
    state.record_error(
        "search_operations", "No operation found", {"connector_id": "conn-123", "query": "reset VM"}
    )

    # Should detect similar error
    assert state.has_similar_error(
        "search_operations", {"connector_id": "conn-123", "query": "different query"}
    )

    # Should not detect error for different connector
    assert not state.has_similar_error(
        "search_operations", {"connector_id": "conn-456", "query": "reset VM"}
    )


@pytest.mark.unit
def test_session_state_add_correlation():
    """Test adding entity correlations"""
    state = AgentSessionState()

    # Correlate VM with pod
    state.add_correlation("vm-107", "pod-abc-123")
    state.add_correlation("vm-107", "k8s-node-1")

    related = state.get_related_entities("vm-107")
    assert len(related) == 2
    assert "pod-abc-123" in related
    assert "k8s-node-1" in related


@pytest.mark.unit
def test_session_state_mark_slow_endpoint():
    """Test marking slow endpoints"""
    state = AgentSessionState()

    state.mark_slow_endpoint("endpoint-123")

    assert "endpoint-123" in state.slow_endpoints


@pytest.mark.unit
def test_session_state_mark_rate_limited():
    """Test marking rate-limited endpoints"""
    state = AgentSessionState()

    state.mark_rate_limited("endpoint-456")

    # Should be rate-limited immediately
    assert state.is_rate_limited("endpoint-456", cooldown_seconds=60)

    # Mock older timestamp
    state.rate_limited_endpoints["endpoint-456"] = datetime.now(tz=UTC) - timedelta(seconds=120)

    # Should not be rate-limited after cooldown
    assert not state.is_rate_limited("endpoint-456", cooldown_seconds=60)


@pytest.mark.unit
def test_session_state_get_context_summary():
    """Test generating context summary"""
    state = AgentSessionState()

    # Empty state
    summary = state.get_context_summary()
    assert summary == "No active context"

    # Add connectors
    state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    state.get_or_create_connector("conn-456", "Kubernetes", "kubernetes")

    # Add operation
    state.set_operation(OperationType.DIAGNOSIS, "Find slow VMs")

    summary = state.get_context_summary()
    assert "vCenter" in summary
    assert "Kubernetes" in summary
    assert "Find slow VMs" in summary


@pytest.mark.unit
def test_session_state_clear_stale_data():
    """Test clearing stale data"""
    state = AgentSessionState()

    # Add connector with old data
    ctx = state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    ctx.recent_data["old"] = {
        "data": {"test": "value"},
        "retrieved_at": datetime.now(tz=UTC) - timedelta(hours=2),
    }
    ctx.recent_data["fresh"] = {"data": {"test": "value"}, "retrieved_at": datetime.now(tz=UTC)}

    # Add old error
    state.recent_errors = [
        {
            "tool": "test",
            "error": "test error",
            "context": {},
            "timestamp": datetime.now(tz=UTC) - timedelta(hours=2),
        }
    ]

    # Clear stale data (older than 1 hour)
    state.clear_stale_data(max_age_seconds=3600)

    # Old data should be removed
    assert "old" not in ctx.recent_data
    assert "fresh" in ctx.recent_data
    assert len(state.recent_errors) == 0


@pytest.mark.unit
def test_session_state_to_dict():
    """Test serializing state to dictionary"""
    state = AgentSessionState()

    # Add some data
    state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    state.primary_connector_id = "conn-123"
    state.set_operation(OperationType.DIAGNOSIS, "Test operation")

    data = state.to_dict()

    assert "connectors" in data
    assert "conn-123" in data["connectors"]
    assert data["primary_connector_id"] == "conn-123"
    assert data["operation_type"] == "diagnosis"
    assert data["operation_goal"] == "Test operation"


@pytest.mark.unit
def test_session_state_from_dict():
    """Test deserializing state from dictionary"""
    # Create state and serialize
    state1 = AgentSessionState()
    ctx = state1.get_or_create_connector("conn-123", "vCenter", "vcenter")
    ctx.add_endpoint("/api/vm", "ep-1", "GET")
    state1.primary_connector_id = "conn-123"
    state1.set_operation(OperationType.DIAGNOSIS, "Test")

    data = state1.to_dict()

    # Deserialize to new state
    state2 = AgentSessionState.from_dict(data)

    assert state2.primary_connector_id == "conn-123"
    assert state2.operation_type == OperationType.DIAGNOSIS
    assert state2.operation_goal == "Test"
    assert "conn-123" in state2.connectors
    assert state2.connectors["conn-123"].connector_name == "vCenter"


@pytest.mark.unit
def test_session_state_multiple_connectors_independent():
    """Test that multiple connectors maintain independent state"""
    state = AgentSessionState()

    # Create two connectors
    ctx1 = state.get_or_create_connector("conn-1", "vCenter", "vcenter")
    ctx2 = state.get_or_create_connector("conn-2", "Kubernetes", "kubernetes")

    # Add endpoints to each
    ctx1.add_endpoint("/api/vm", "ep-1", "GET")
    ctx2.add_endpoint("/api/pod", "ep-2", "GET")

    # Verify independence
    assert ctx1.get_endpoint("/api/vm", "GET") == "ep-1"
    assert ctx1.get_endpoint("/api/pod", "GET") is None

    assert ctx2.get_endpoint("/api/pod", "GET") == "ep-2"
    assert ctx2.get_endpoint("/api/vm", "GET") is None
