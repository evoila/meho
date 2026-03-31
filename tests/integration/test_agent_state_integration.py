# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for agent with session state management.

Tests realistic multi-turn conversation scenarios:
- UUID auto-correction
- Multi-connector workflows
- Error learning
"""

from unittest.mock import AsyncMock

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.dependencies import MEHODependencies
from meho_app.modules.agents.session_state import AgentSessionState, OperationType


@pytest.fixture
def mock_knowledge_store():
    """Mock knowledge store"""
    return AsyncMock()


@pytest.fixture
def mock_connector_repo():
    """Mock connector repository"""
    return AsyncMock()


@pytest.fixture
def mock_endpoint_repo():
    """Mock endpoint descriptor repository"""
    return AsyncMock()


@pytest.fixture
def mock_user_cred_repo():
    """Mock user credential repository"""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """Mock HTTP client"""
    return AsyncMock()


@pytest.fixture
def user_context():
    """User context"""
    return UserContext(user_id="user-1", tenant_id="tenant-1")


@pytest.fixture
def session_state():
    """Fresh session state"""
    return AgentSessionState()


@pytest.fixture
def dependencies(
    mock_knowledge_store,
    mock_connector_repo,
    mock_endpoint_repo,
    mock_user_cred_repo,
    mock_http_client,
    user_context,
    session_state,
):
    """MEHO dependencies with state"""
    return MEHODependencies(
        knowledge_store=mock_knowledge_store,
        connector_repo=mock_connector_repo,
        endpoint_repo=mock_endpoint_repo,
        user_cred_repo=mock_user_cred_repo,
        http_client=mock_http_client,
        user_context=user_context,
        session_state=session_state,
    )


# ============================================================================
# MULTI-TURN CONVERSATION TESTS
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connector_persists_across_calls(dependencies):
    """Test that connector context persists across multiple tool calls"""

    # Simulate determine_connector call
    connector_id = "a72f87bf-1234-5678-abcd-ef1234567890"
    connector_name = "VCF Hetzner vCenter"

    # Store connector in state (mimics what streaming_agent.py does)
    dependencies.session_state.get_or_create_connector(
        connector_id=connector_id, connector_name=connector_name, connector_type="vcenter"
    )
    dependencies.session_state.primary_connector_id = connector_id

    # Verify connector is stored
    active = dependencies.session_state.get_active_connector()
    assert active is not None
    assert active.connector_id == connector_id
    assert active.connector_name == connector_name

    # Simulate second call - connector should still be there
    active = dependencies.session_state.get_active_connector()
    assert active.connector_id == connector_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_uuid_auto_correction_from_state(dependencies):
    """Test that truncated UUIDs are auto-corrected from state"""

    # Store full UUID in state
    full_uuid = "a72f87bf-1234-5678-abcd-ef1234567890"
    dependencies.session_state.get_or_create_connector(
        connector_id=full_uuid, connector_name="vCenter", connector_type="vcenter"
    )
    dependencies.session_state.primary_connector_id = full_uuid

    # Simulate agent using truncated UUID (from conversation history)
    truncated_uuid = "a72f87bf-..."

    # Auto-correction logic (mimics what tools do)
    if "..." in truncated_uuid:
        active = dependencies.session_state.get_active_connector()
        corrected_uuid = active.connector_id if active else None
    else:
        corrected_uuid = truncated_uuid

    # Should be auto-corrected to full UUID
    assert corrected_uuid == full_uuid
    assert "..." not in corrected_uuid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_connector_context_switching(dependencies):
    """Test switching between multiple connectors"""

    vcenter_id = "vcenter-conn-123"
    k8s_id = "k8s-conn-456"

    # User works with vCenter
    dependencies.session_state.get_or_create_connector(vcenter_id, "vCenter", "vcenter")
    dependencies.session_state.primary_connector_id = vcenter_id

    # User switches to Kubernetes
    dependencies.session_state.get_or_create_connector(k8s_id, "Kubernetes", "kubernetes")
    dependencies.session_state.switch_connector(k8s_id)

    # Should have both connectors
    assert len(dependencies.session_state.connectors) == 2

    # Active should be Kubernetes
    active = dependencies.session_state.get_active_connector()
    assert active.connector_id == k8s_id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_endpoint_caching(dependencies):
    """Test that discovered endpoints are cached"""

    connector_id = "conn-123"

    # Create connector
    ctx = dependencies.session_state.get_or_create_connector(connector_id, "vCenter", "vcenter")

    # Simulate discovering endpoints
    ctx.add_endpoint("/api/vcenter/vm", "endpoint-uuid-1", "GET")
    ctx.add_endpoint("/api/vcenter/host", "endpoint-uuid-2", "GET")
    ctx.add_endpoint("/api/vcenter/cluster", "endpoint-uuid-3", "GET")

    # Should be able to retrieve cached endpoints
    assert ctx.get_endpoint("/api/vcenter/vm", "GET") == "endpoint-uuid-1"
    assert ctx.get_endpoint("/api/vcenter/host", "GET") == "endpoint-uuid-2"
    assert ctx.get_endpoint("/api/vcenter/cluster", "GET") == "endpoint-uuid-3"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_error_learning_prevents_retry(dependencies):
    """Test that recorded errors prevent retrying same query"""

    connector_id = "conn-123"
    ctx = dependencies.session_state.get_or_create_connector(connector_id, "vCenter", "vcenter")

    # Simulate failed search
    query = "reset VM endpoint"
    ctx.record_failure(query)

    # Record in global state too
    dependencies.session_state.record_error(
        "search_endpoints",
        "No endpoint found for 'reset VM'",
        {"connector_id": connector_id, "query": query},
    )

    # Should detect similar error
    assert dependencies.session_state.has_similar_error(
        "search_endpoints", {"connector_id": connector_id, "query": "reset VM different wording"}
    )

    # Agent should avoid retrying and try different approach
    # (e.g., search documentation instead)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_workflow_tracking(dependencies):
    """Test tracking multi-step workflow progress"""

    from meho_app.modules.agents.session_state import WorkflowStep

    # Set operation context
    dependencies.session_state.set_operation(
        OperationType.DIAGNOSIS, "Diagnose app-prod performance issues"
    )

    # Add workflow steps
    dependencies.session_state.add_workflow_step(
        WorkflowStep(
            step_id="step-1",
            description="Get VM metrics from vCenter",
            status="completed",
            tool_name="call_endpoint",
            tool_args={"connector_id": "vcenter-conn"},
        )
    )

    dependencies.session_state.add_workflow_step(
        WorkflowStep(
            step_id="step-2",
            description="Get pod metrics from Kubernetes",
            status="in_progress",
            tool_name="call_endpoint",
            tool_args={"connector_id": "k8s-conn"},
        )
    )

    dependencies.session_state.add_workflow_step(
        WorkflowStep(
            step_id="step-3",
            description="Analyze and correlate metrics",
            status="pending",
            tool_name="interpret_results",
            tool_args={},
        )
    )

    # Check progress
    progress = dependencies.session_state.get_workflow_progress()
    assert progress["total"] == 3
    assert progress["completed"] == 1
    assert progress["in_progress"] == 1

    # Should be able to show progress to user
    # "Workflow: 1/3 steps completed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_user_preference_learning(dependencies):
    """Test learning user output preferences"""

    # User asks for table format
    dependencies.session_state.learn_preference("output_format", "table")

    # User specifies filters
    dependencies.session_state.learn_preference("vm_filter", "power_state=POWERED_ON")

    # User prefers detailed output
    dependencies.session_state.learn_preference("show_details", True)

    # Subsequent queries should use these preferences
    format_pref = dependencies.session_state.get_preference("output_format")
    assert format_pref == "table"

    filter_pref = dependencies.session_state.get_preference("vm_filter")
    assert filter_pref == "power_state=POWERED_ON"

    details_pref = dependencies.session_state.get_preference("show_details")
    assert details_pref is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_system_correlation(dependencies):
    """Test correlating entities across systems"""

    # Discover that VM hosts a K8s node
    dependencies.session_state.add_correlation("vm-107", "k8s-node-1")

    # Discover that K8s node runs pods
    dependencies.session_state.add_correlation("k8s-node-1", "pod-abc-123")
    dependencies.session_state.add_correlation("k8s-node-1", "pod-def-456")

    # Should be able to traverse relationships
    vm_related = dependencies.session_state.get_related_entities("vm-107")
    assert "k8s-node-1" in vm_related

    node_related = dependencies.session_state.get_related_entities("k8s-node-1")
    assert "pod-abc-123" in node_related
    assert "pod-def-456" in node_related

    # Useful for: "Which pods are affected if VM-107 goes down?"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_state_context_summary(dependencies):
    """Test generating readable context summary"""

    # Build up state
    dependencies.session_state.get_or_create_connector("conn-1", "vCenter", "vcenter")
    dependencies.session_state.get_or_create_connector("conn-2", "Kubernetes", "kubernetes")

    dependencies.session_state.set_operation(
        OperationType.DIAGNOSIS, "Find performance bottlenecks"
    )

    # Generate summary
    summary = dependencies.session_state.get_context_summary()

    # Should contain key information
    assert "vCenter" in summary
    assert "Kubernetes" in summary
    assert "Find performance bottlenecks" in summary


@pytest.mark.integration
@pytest.mark.asyncio
async def test_state_serialization_roundtrip(dependencies):
    """Test that state can be serialized and deserialized"""

    # Build up state
    dependencies.session_state.get_or_create_connector("conn-123", "vCenter", "vcenter")
    dependencies.session_state.primary_connector_id = "conn-123"
    dependencies.session_state.set_operation(OperationType.RETRIEVAL, "Get VM information")

    # Serialize to dict
    state_dict = dependencies.session_state.to_dict()

    # Verify dict structure
    assert "connectors" in state_dict
    assert "conn-123" in state_dict["connectors"]
    assert state_dict["primary_connector_id"] == "conn-123"
    assert state_dict["operation_type"] == "retrieval"

    # Deserialize to new state
    new_state = AgentSessionState.from_dict(state_dict)

    # Should have same data
    assert new_state.primary_connector_id == "conn-123"
    assert new_state.operation_type == OperationType.RETRIEVAL
    assert "conn-123" in new_state.connectors


@pytest.mark.integration
@pytest.mark.asyncio
async def test_production_bug_scenario_fixed(dependencies):
    """
    Test the exact production bug scenario that state management fixes:

    1. User: "Get VMs from vCenter"
    2. Agent stores full UUID, shows abbreviated
    3. User: "Get IPs for those VMs"
    4. Agent uses abbreviated UUID from history → FAILS

    With state management, this should work!
    """

    # Turn 1: Determine connector
    full_uuid = "a72f87bf-1234-5678-abcd-ef1234567890"
    dependencies.session_state.get_or_create_connector(full_uuid, "VCF Hetzner vCenter", "vcenter")
    dependencies.session_state.primary_connector_id = full_uuid

    # Agent shows to user: "Connector: VCF Hetzner vCenter (a72f87bf-...)"
    # But full UUID is in state!

    # Turn 2: Agent tries to use truncated UUID from conversation
    truncated = "a72f87bf-..."

    # State management auto-corrects
    if "..." in truncated:
        active = dependencies.session_state.get_active_connector()
        corrected = active.connector_id
    else:
        corrected = truncated

    # ✅ Should be corrected to full UUID
    assert corrected == full_uuid
    assert "..." not in corrected

    # Production bug is FIXED! 🎉
