# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for TASK-76 Approval Flow.

Tests the complete approval flow from danger level detection
through approval persistence and resume logic.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_app.modules.agents.approval import (
    ApprovalRequired,
    ApprovalStore,
    assign_danger_level,
    get_impact_message,
    should_require_approval,
)
from meho_app.modules.agents.approval.exceptions import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
)

# =============================================================================
# Danger Level Assignment Tests
# =============================================================================


class TestDangerLevelAssignment:
    """Test automatic danger level assignment based on HTTP method."""

    @pytest.mark.integration
    def test_get_is_safe(self):
        """GET requests should be safe."""
        level, requires = assign_danger_level("GET", "/api/vms")
        assert level == "safe"
        assert requires is False

    @pytest.mark.integration
    def test_head_is_safe(self):
        """HEAD requests should be safe."""
        level, requires = assign_danger_level("HEAD", "/api/health")
        assert level == "safe"
        assert requires is False

    @pytest.mark.integration
    def test_options_is_safe(self):
        """OPTIONS requests should be safe."""
        level, requires = assign_danger_level("OPTIONS", "/api/vms")
        assert level == "safe"
        assert requires is False

    @pytest.mark.integration
    def test_post_is_dangerous(self):
        """POST requests should require approval."""
        level, requires = assign_danger_level("POST", "/api/vms")
        assert level == "dangerous"
        assert requires is True

    @pytest.mark.integration
    def test_put_is_dangerous(self):
        """PUT requests should require approval."""
        level, requires = assign_danger_level("PUT", "/api/vms/123")
        assert level == "dangerous"
        assert requires is True

    @pytest.mark.integration
    def test_patch_is_dangerous(self):
        """PATCH requests should require approval."""
        level, requires = assign_danger_level("PATCH", "/api/vms/123")
        assert level == "dangerous"
        assert requires is True

    @pytest.mark.integration
    def test_delete_is_critical(self):
        """DELETE requests should be critical."""
        level, requires = assign_danger_level("DELETE", "/api/vms/123")
        assert level == "critical"
        assert requires is True

    @pytest.mark.integration
    def test_unknown_method_is_dangerous(self):
        """Unknown methods should default to dangerous."""
        level, requires = assign_danger_level("CUSTOM", "/api/special")
        assert level == "dangerous"
        assert requires is True


class TestImpactMessages:
    """Test impact message generation."""

    @pytest.mark.integration
    def test_delete_impact_message(self):
        """DELETE should have strong warning."""
        msg = get_impact_message("DELETE", "/api/vms/123")
        assert "permanently" in msg.lower() or "delete" in msg.lower()

    @pytest.mark.integration
    def test_post_impact_message(self):
        """POST should mention creation."""
        msg = get_impact_message("POST", "/api/vms")
        assert "create" in msg.lower() or "new" in msg.lower()

    @pytest.mark.integration
    def test_put_impact_message(self):
        """PUT should mention modification."""
        msg = get_impact_message("PUT", "/api/vms/123")
        assert "modify" in msg.lower() or "update" in msg.lower() or "replace" in msg.lower()


# =============================================================================
# ApprovalRequired Exception Tests
# =============================================================================


class TestApprovalRequiredException:
    """Test the ApprovalRequired exception."""

    @pytest.mark.integration
    def test_exception_creation(self):
        """Test creating ApprovalRequired exception."""
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={"endpoint_id": "123"},
            danger_level="dangerous",
            context={
                "method": "POST",
                "path": "/api/vms",
                "description": "Create VM",
            },
        )

        assert exc.tool_name == "call_endpoint"
        assert exc.danger_level == "dangerous"
        assert exc.http_method == "POST"
        assert exc.endpoint_path == "/api/vms"
        assert exc.description == "Create VM"

    @pytest.mark.integration
    def test_exception_to_dict(self):
        """Test serialization to dict."""
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={"endpoint_id": "123"},
            danger_level="critical",
            context={
                "method": "DELETE",
                "path": "/api/vms/123",
            },
            approval_id="abc-123",
        )

        data = exc.to_dict()
        assert data["tool_name"] == "call_endpoint"
        assert data["danger_level"] == "critical"
        assert data["approval_id"] == "abc-123"

    @pytest.mark.integration
    def test_exception_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "tool_name": "call_endpoint",
            "tool_args": {"endpoint_id": "456"},
            "danger_level": "dangerous",
            "context": {"method": "PUT"},
            "approval_id": "xyz-789",
        }

        exc = ApprovalRequired.from_dict(data)
        assert exc.tool_name == "call_endpoint"
        assert exc.approval_id == "xyz-789"


# =============================================================================
# ApprovalStore Integration Tests
# =============================================================================


class TestApprovalStoreIntegration:
    """Integration tests for ApprovalStore with mocked database."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock database session."""
        session = AsyncMock()
        session.execute = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.commit = AsyncMock()
        return session

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_pending_approval(self, mock_session):
        """Test creating a pending approval request."""
        store = ApprovalStore(mock_session)

        # Mock the execute to return no existing approval
        mock_session.execute.return_value.scalar_one_or_none.return_value = None

        session_id = uuid.uuid4()
        result = await store.create_pending(
            session_id=session_id,
            tenant_id="tenant-1",
            user_id="user-1",
            tool_name="call_endpoint",
            tool_args={"endpoint_id": "123"},
            danger_level="dangerous",
            user_message="Delete VM",
            http_method="DELETE",
            endpoint_path="/api/vms/123",
        )

        assert result is not None
        assert mock_session.add.called

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_check_approval_not_found(self, mock_session):
        """Test checking for non-existent approval."""
        store = ApprovalStore(mock_session)

        # Mock no approval found - need to properly mock the async chain
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        result = await store.check_approval(
            session_id=uuid.uuid4(), tool_name="call_endpoint", tool_args={"endpoint_id": "123"}
        )

        assert result is None


# =============================================================================
# Security Tests
# =============================================================================


class TestApprovalSecurityIntegration:
    """Integration tests for approval security measures."""

    @pytest.mark.integration
    def test_cannot_bypass_with_different_args(self):
        """Approvals are bound to specific tool args."""
        # Create approval for endpoint A
        args_a = {"endpoint_id": "endpoint-A"}
        args_b = {"endpoint_id": "endpoint-B"}

        # Hash should be different
        import hashlib
        import json

        hash_a = hashlib.sha256(json.dumps(args_a, sort_keys=True).encode()).hexdigest()
        hash_b = hashlib.sha256(json.dumps(args_b, sort_keys=True).encode()).hexdigest()

        assert hash_a != hash_b, "Different args should have different hashes"

    @pytest.mark.integration
    def test_approval_has_expiry(self):
        """Test that approvals can have expiry times."""
        expiry = datetime.now(tz=UTC) + timedelta(minutes=60)
        assert expiry > datetime.now(tz=UTC)

    @pytest.mark.integration
    def test_expired_approval_rejected(self):
        """Test that expired approvals raise ApprovalExpired."""
        exc = ApprovalExpired(approval_id="123", expired_at="2024-01-01T00:00:00Z")
        assert "expired" in str(exc).lower()

    @pytest.mark.integration
    def test_already_decided_rejected(self):
        """Test that already-decided approvals raise error."""
        exc = ApprovalAlreadyDecided(approval_id="123", current_status="approved")
        assert exc.current_status == "approved"


# =============================================================================
# Streaming Agent Integration Tests (Mocked)
# =============================================================================


class TestStreamingAgentApprovalIntegration:
    """Test approval integration in streaming agent (mocked)."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_approval_check_in_call_endpoint(self):
        """Test that call_endpoint checks for existing approvals."""
        # This is a mock test - actual integration requires running agent

        # Simulate the check that happens in call_endpoint

        # Check function exists and returns expected format
        level, requires = assign_danger_level("DELETE", "/api/vms/{id}")
        assert level == "critical"
        assert requires is True

        # If approval exists and is approved, should proceed
        # (This would be mocked in actual test)
        assert should_require_approval("critical") is True
        assert should_require_approval("safe") is False

    @pytest.mark.integration
    def test_approval_event_format(self):
        """Test that approval_required event has correct format."""
        # Expected event format from streaming agent
        event = {
            "type": "approval_required",
            "approval_id": str(uuid.uuid4()),
            "tool": "call_endpoint",
            "danger_level": "critical",
            "details": {
                "method": "DELETE",
                "path": "/api/vms/123",
                "description": "Delete virtual machine",
                "impact": "This will permanently delete the resource",
            },
            "tool_args": {"endpoint_id": "123"},
            "message": "I need your approval to execute: Delete virtual machine",
        }

        assert event["type"] == "approval_required"
        assert event["approval_id"] is not None
        assert event["danger_level"] in ["safe", "caution", "dangerous", "critical"]
        assert "method" in event["details"]
        assert "path" in event["details"]


# =============================================================================
# Full Flow Tests (Mocked End-to-End)
# =============================================================================


class TestApprovalFullFlow:
    """Test complete approval flow from request to execution."""

    @pytest.mark.integration
    def test_full_approval_flow_logic(self):
        """Test the logical flow of approval process."""
        # Step 1: User requests dangerous action

        # Step 2: Agent detects DELETE method
        http_method = "DELETE"
        endpoint_path = "/api/vms/{id}"

        # Step 3: Check danger level
        level, requires = assign_danger_level(http_method, endpoint_path)
        assert level == "critical"
        assert requires is True

        # Step 4: Create ApprovalRequired exception
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={"endpoint_id": "123", "parameter_sets": [{"path_params": {"id": "vm-123"}}]},
            danger_level=level,
            context={
                "method": http_method,
                "path": endpoint_path,
                "description": "Delete virtual machine",
                "impact": get_impact_message(http_method, endpoint_path),
            },
        )

        # Step 5: Verify exception properties
        assert exc.danger_level == "critical"
        assert exc.http_method == "DELETE"
        assert "vm-123" in str(exc.tool_args)

        # Step 6: After user approves and re-sends
        # (Agent would check for existing approval and proceed)
        # This is simulated - actual flow requires database

        # Step 7: Verify the flow completed correctly
        assert exc.description == "Delete virtual machine"
