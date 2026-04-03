# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for approval module (TASK-76).

Tests:
- ApprovalRequired exception
- Danger level assignment
- ApprovalStore repository (with mocked DB)
"""

import pytest

from meho_app.modules.agents.approval.danger_level import (
    assign_danger_level,
    get_danger_emoji,
    get_impact_message,
    is_safe_post_pattern,
    should_auto_approve,
    should_require_approval,
)
from meho_app.modules.agents.approval.exceptions import (
    ApprovalAlreadyDecided,
    ApprovalExpired,
    ApprovalNotFound,
    ApprovalRequired,
)

# =============================================================================
# DANGER LEVEL TESTS
# =============================================================================


class TestDangerLevelAssignment:
    """Tests for automatic danger level assignment."""

    def test_get_methods_are_safe(self):
        """GET methods should be safe and auto-approved."""
        level, requires = assign_danger_level("GET", "/api/vm")
        assert level == "safe"
        assert requires is False

    def test_head_methods_are_safe(self):
        """HEAD methods should be safe and auto-approved."""
        level, requires = assign_danger_level("HEAD", "/api/vm")
        assert level == "safe"
        assert requires is False

    def test_options_methods_are_safe(self):
        """OPTIONS methods should be safe and auto-approved."""
        level, requires = assign_danger_level("OPTIONS", "/api/vm")
        assert level == "safe"
        assert requires is False

    def test_post_methods_are_dangerous(self):
        """POST methods should be dangerous and require approval."""
        level, requires = assign_danger_level("POST", "/api/vm")
        assert level == "dangerous"
        assert requires is True

    def test_put_methods_are_dangerous(self):
        """PUT methods should be dangerous and require approval."""
        level, requires = assign_danger_level("PUT", "/api/vm/123")
        assert level == "dangerous"
        assert requires is True

    def test_patch_methods_are_dangerous(self):
        """PATCH methods should be dangerous and require approval."""
        level, requires = assign_danger_level("PATCH", "/api/vm/123")
        assert level == "dangerous"
        assert requires is True

    def test_delete_methods_are_critical(self):
        """DELETE methods should be critical and require approval."""
        level, requires = assign_danger_level("DELETE", "/api/vm/123")
        assert level == "critical"
        assert requires is True

    def test_case_insensitive(self):
        """Method comparison should be case-insensitive."""
        level1, _ = assign_danger_level("get", "/api/vm")
        level2, _ = assign_danger_level("GET", "/api/vm")
        level3, _ = assign_danger_level("Get", "/api/vm")

        assert level1 == level2 == level3 == "safe"

    def test_unknown_method_is_dangerous(self):
        """Unknown HTTP methods should default to dangerous."""
        level, requires = assign_danger_level("CUSTOM", "/api/vm")
        assert level == "dangerous"
        assert requires is True

    def test_override_takes_precedence(self):
        """Manual override should take precedence over auto-assignment."""
        # Override GET to dangerous
        level, requires = assign_danger_level("GET", "/api/vm", override="dangerous")
        assert level == "dangerous"
        assert requires is True

        # Override DELETE to safe
        level, requires = assign_danger_level("DELETE", "/api/vm", override="safe")
        assert level == "safe"
        assert requires is False


class TestDangerLevelHelpers:
    """Tests for danger level helper functions."""

    def test_should_require_approval(self):
        """Test approval requirement logic."""
        assert should_require_approval("safe") is False
        assert should_require_approval("caution") is False
        assert should_require_approval("dangerous") is True
        assert should_require_approval("critical") is True

    def test_should_auto_approve(self):
        """Test auto-approval logic."""
        assert should_auto_approve("safe") is True
        assert should_auto_approve("caution") is True
        assert should_auto_approve("dangerous") is False
        assert should_auto_approve("critical") is False

    def test_get_impact_message_delete(self):
        """DELETE should have strong warning message."""
        msg = get_impact_message("DELETE", "/api/vm")
        assert "permanently delete" in msg.lower() or "cannot be undone" in msg.lower()

    def test_get_impact_message_post(self):
        """POST should mention resource creation."""
        msg = get_impact_message("POST", "/api/vm")
        assert "create" in msg.lower()

    def test_get_danger_emoji(self):
        """Test emoji mapping."""
        assert get_danger_emoji("safe") == "🟢"
        assert get_danger_emoji("caution") == "🟡"
        assert get_danger_emoji("dangerous") == "🟠"
        assert get_danger_emoji("critical") == "🔴"

    def test_is_safe_post_pattern(self):
        """Test safe POST pattern detection."""
        assert is_safe_post_pattern("/api/auth/login") is True
        assert is_safe_post_pattern("/api/auth/token") is True
        assert is_safe_post_pattern("/api/session") is True
        assert is_safe_post_pattern("/api/vcenter/vm?action=list") is True
        assert is_safe_post_pattern("/api/vm/create") is False


# =============================================================================
# APPROVAL REQUIRED EXCEPTION TESTS
# =============================================================================


class TestApprovalRequiredException:
    """Tests for ApprovalRequired exception."""

    def test_basic_creation(self):
        """Test basic exception creation."""
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={"endpoint_id": "123"},
            danger_level="dangerous",
            context={"method": "POST", "path": "/api/vm"},
        )

        assert exc.tool_name == "call_endpoint"
        assert exc.tool_args == {"endpoint_id": "123"}
        assert exc.danger_level == "dangerous"

    def test_context_properties(self):
        """Test context property accessors."""
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={},
            context={
                "method": "DELETE",
                "path": "/api/vm/123",
                "description": "Delete VM",
                "impact": "Cannot be undone",
            },
        )

        assert exc.http_method == "DELETE"
        assert exc.endpoint_path == "/api/vm/123"
        assert exc.description == "Delete VM"
        assert exc.impact_message == "Cannot be undone"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={"endpoint_id": "123"},
            danger_level="critical",
            context={"method": "DELETE"},
        )

        data = exc.to_dict()

        assert data["tool_name"] == "call_endpoint"
        assert data["tool_args"] == {"endpoint_id": "123"}
        assert data["danger_level"] == "critical"
        assert data["context"] == {"method": "DELETE"}

    def test_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "tool_name": "call_endpoint",
            "tool_args": {"endpoint_id": "456"},
            "danger_level": "dangerous",
            "context": {"method": "POST"},
            "approval_id": "abc-123",
        }

        exc = ApprovalRequired.from_dict(data)

        assert exc.tool_name == "call_endpoint"
        assert exc.tool_args == {"endpoint_id": "456"}
        assert exc.danger_level == "dangerous"
        assert exc.approval_id == "abc-123"

    def test_exception_message(self):
        """Test that exception has meaningful message."""
        exc = ApprovalRequired(
            tool_name="call_endpoint",
            tool_args={},
            context={"description": "Delete virtual machine"},
        )

        # Should be able to convert to string
        msg = str(exc)
        assert "call_endpoint" in msg
        assert "Delete virtual machine" in msg


class TestOtherExceptions:
    """Tests for other approval exceptions."""

    def test_approval_expired(self):
        """Test ApprovalExpired exception."""
        exc = ApprovalExpired("approval-123", "2025-12-01 10:00:00")
        assert exc.approval_id == "approval-123"
        assert "expired" in str(exc).lower()

    def test_approval_not_found(self):
        """Test ApprovalNotFound exception."""
        exc = ApprovalNotFound("approval-456")
        assert exc.approval_id == "approval-456"
        assert "not found" in str(exc).lower()

    def test_approval_already_decided(self):
        """Test ApprovalAlreadyDecided exception."""
        exc = ApprovalAlreadyDecided("approval-789", "approved")
        assert exc.approval_id == "approval-789"
        assert exc.current_status == "approved"
        assert "already" in str(exc).lower()


# =============================================================================
# APPROVAL STORE TESTS (with mocked DB)
# =============================================================================


class TestApprovalStoreHasher:
    """Tests for the args hasher utility."""

    def test_hash_is_deterministic(self):
        """Same args should produce same hash."""
        from meho_app.modules.agents.approval.repository import ApprovalStore

        args = {"endpoint_id": "123", "params": {"name": "test"}}

        hash1 = ApprovalStore._hash_tool_args(args)
        hash2 = ApprovalStore._hash_tool_args(args)

        assert hash1 == hash2

    def test_hash_is_order_independent(self):
        """Key order shouldn't affect hash."""
        from meho_app.modules.agents.approval.repository import ApprovalStore

        args1 = {"a": 1, "b": 2}
        args2 = {"b": 2, "a": 1}

        hash1 = ApprovalStore._hash_tool_args(args1)
        hash2 = ApprovalStore._hash_tool_args(args2)

        assert hash1 == hash2

    def test_different_args_different_hash(self):
        """Different args should produce different hashes."""
        from meho_app.modules.agents.approval.repository import ApprovalStore

        args1 = {"endpoint_id": "123"}
        args2 = {"endpoint_id": "456"}

        hash1 = ApprovalStore._hash_tool_args(args1)
        hash2 = ApprovalStore._hash_tool_args(args2)

        assert hash1 != hash2


# =============================================================================
# INTEGRATION-STYLE TESTS (would need real DB in integration/)
# =============================================================================


class TestApprovalStoreContract:
    """
    Contract tests for ApprovalStore.

    These test the public interface without a real database.
    Full integration tests with real DB are in tests/integration/.
    """

    @pytest.mark.asyncio
    def test_create_pending_interface(self):
        """Test create_pending has correct interface."""
        from meho_app.modules.agents.approval.repository import ApprovalStore

        # Verify the method exists and has expected signature
        assert hasattr(ApprovalStore, "create_pending")

        # Check it's async
        import inspect

        assert inspect.iscoroutinefunction(ApprovalStore.create_pending)

    @pytest.mark.asyncio
    def test_approve_interface(self):
        """Test approve has correct interface."""
        from meho_app.modules.agents.approval.repository import ApprovalStore

        assert hasattr(ApprovalStore, "approve")

        import inspect

        assert inspect.iscoroutinefunction(ApprovalStore.approve)

    @pytest.mark.asyncio
    def test_reject_interface(self):
        """Test reject has correct interface."""
        from meho_app.modules.agents.approval.repository import ApprovalStore

        assert hasattr(ApprovalStore, "reject")

        import inspect

        assert inspect.iscoroutinefunction(ApprovalStore.reject)
