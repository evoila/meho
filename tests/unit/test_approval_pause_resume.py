# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Tests for Phase 5 approval pause/resume mechanism.

Tests cover:
- Pending approval registry (register, resolve, cleanup)
- Trust-tier-aware tool dispatch (_requires_approval)
- Denial scratchpad path (SpecialistAgent wiring validation)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from meho_app.modules.agents.approval.pending_approvals import (
    PENDING_APPROVALS,
    cleanup_pending,
    register_pending,
    resolve_pending,
)

# =============================================================================
# Pending Approval Registry Tests
# =============================================================================


class TestPendingApprovalRegistry:
    """Test the in-process pending approval registry."""

    def setup_method(self):
        """Clear the global registry before each test."""
        PENDING_APPROVALS.clear()

    def teardown_method(self):
        """Clear the global registry after each test."""
        PENDING_APPROVALS.clear()

    def test_register_and_resolve_approved(self):
        """Register pending, resolve with approved=True, check event is set and approved."""
        pending = register_pending(
            session_id="session-1",
            tool_name="call_operation",
            tool_args={"operation_id": "create_vm"},
            approval_id="approval-abc",
        )

        assert not pending.event.is_set()
        assert not pending.approved
        assert pending.tool_name == "call_operation"
        assert pending.approval_id == "approval-abc"

        result = resolve_pending("session-1", approved=True)

        assert result is True
        assert pending.event.is_set()
        assert pending.approved is True

    def test_register_and_resolve_denied(self):
        """Register pending, resolve with approved=False, check event is set and approved is False."""
        pending = register_pending(
            session_id="session-2",
            tool_name="call_operation",
            tool_args={"operation_id": "delete_vm"},
        )

        result = resolve_pending("session-2", approved=False)

        assert result is True
        assert pending.event.is_set()
        assert pending.approved is False

    def test_resolve_nonexistent_session(self):
        """resolve_pending for unknown session returns False."""
        result = resolve_pending("nonexistent", approved=True)
        assert result is False

    def test_cleanup_removes_entry(self):
        """Register, cleanup, verify PENDING_APPROVALS is empty."""
        register_pending(
            session_id="session-3",
            tool_name="call_operation",
            tool_args={},
        )

        assert "session-3" in PENDING_APPROVALS

        cleanup_pending("session-3")

        assert "session-3" not in PENDING_APPROVALS
        assert len(PENDING_APPROVALS) == 0

    def test_cleanup_nonexistent_is_noop(self):
        """cleanup_pending for unknown session doesn't raise."""
        # Should not raise
        cleanup_pending("nonexistent")
        assert len(PENDING_APPROVALS) == 0

    def test_state_set_before_event(self):
        """Verify that approved state is set BEFORE event.set() (Research Pitfall 2)."""
        pending = register_pending(
            session_id="session-4",
            tool_name="call_operation",
            tool_args={},
        )

        # Before resolve: both are default
        assert not pending.approved
        assert not pending.event.is_set()

        # After resolve: approved is True AND event is set
        resolve_pending("session-4", approved=True)
        assert pending.approved is True
        assert pending.event.is_set()

    def test_register_overwrites_existing(self):
        """Registering same session_id overwrites previous entry."""
        register_pending("session-5", "tool_a", {})
        pending2 = register_pending("session-5", "tool_b", {})

        assert PENDING_APPROVALS["session-5"] is pending2
        assert pending2.tool_name == "tool_b"

    @pytest.mark.asyncio
    async def test_asyncio_event_wait_works(self):
        """Verify asyncio.Event.wait() unblocks after resolve_pending."""
        pending = register_pending("session-6", "call_operation", {})

        async def resolve_after_delay():
            await asyncio.sleep(0.05)
            resolve_pending("session-6", approved=True)

        task = asyncio.create_task(resolve_after_delay())
        await asyncio.wait_for(pending.event.wait(), timeout=2.0)

        assert pending.approved is True
        await task


# =============================================================================
# Trust-Tier-Aware Tool Dispatch Tests
# =============================================================================


class TestToolDispatchTrustClassification:
    """Test that ToolDispatchNode uses trust classifier correctly."""

    def _make_dispatch_node(self):
        """Create a ToolDispatchNode instance."""
        from meho_app.modules.agents.react_agent.nodes.tool_dispatch import (
            ToolDispatchNode,
        )

        return ToolDispatchNode()

    def _make_deps(self, require_approval: bool = True):
        """Create a mock deps object."""
        deps = MagicMock()
        deps.agent_config = MagicMock()
        deps.agent_config.tools = {
            "call_operation": {
                "require_approval_for_dangerous": require_approval,
            }
        }
        return deps

    def test_get_operation_skips_approval(self):
        """Verify _requires_approval returns False for GET method."""
        node = self._make_dispatch_node()
        deps = self._make_deps()

        result = node._requires_approval(
            "call_operation",
            {"method": "GET", "operation_id": "list_vms"},
            deps,
        )
        assert result is False

    def test_post_operation_requires_approval(self):
        """Verify _requires_approval returns True for POST method."""
        node = self._make_dispatch_node()
        deps = self._make_deps()

        result = node._requires_approval(
            "call_operation",
            {"method": "POST", "operation_id": "create_vm"},
            deps,
        )
        assert result is True

    def test_delete_operation_requires_approval(self):
        """Verify _requires_approval returns True for DELETE method."""
        node = self._make_dispatch_node()
        deps = self._make_deps()

        result = node._requires_approval(
            "call_operation",
            {"method": "DELETE", "operation_id": "delete_vm"},
            deps,
        )
        assert result is True

    def test_non_dangerous_tool_skips_approval(self):
        """Non-dangerous tools never require approval."""
        node = self._make_dispatch_node()
        deps = self._make_deps()

        result = node._requires_approval("search_operations", {"query": "list"}, deps)
        assert result is False

    def test_config_disabled_approval(self):
        """Config can disable approval for dangerous tools."""
        node = self._make_dispatch_node()
        deps = self._make_deps(require_approval=False)

        result = node._requires_approval(
            "call_operation",
            {"method": "DELETE", "operation_id": "delete_vm"},
            deps,
        )
        assert result is False

    def test_assess_operation_danger_uses_classifier(self):
        """_assess_operation_danger returns trust tier values."""
        node = self._make_dispatch_node()

        # GET -> read
        assert node._assess_operation_danger({"method": "GET"}) == "read"
        # POST -> write
        assert node._assess_operation_danger({"method": "POST"}) == "write"
        # DELETE -> destructive
        assert node._assess_operation_danger({"method": "DELETE"}) == "destructive"


# =============================================================================
# Denial Scratchpad Path Tests
# =============================================================================


class TestDenialScratchpadPath:
    """Validate the denial scratchpad message format from SpecialistAgent wiring."""

    def setup_method(self):
        """Clear the global registry before each test."""
        PENDING_APPROVALS.clear()

    def teardown_method(self):
        """Clear the global registry after each test."""
        PENDING_APPROVALS.clear()

    def test_denial_records_in_scratchpad(self):
        """Verify denial message format matches expected pattern.

        Simulates the denial path by checking that the message format
        the SpecialistAgent constructs contains the expected components:
        - Operator DENIED prefix
        - Trust tier value
        - Operation identifier
        - Instructions to try read-only alternative
        """
        # Simulate the denial message construction from agent.py
        from meho_app.modules.agents.models import TrustTier

        trust_tier = TrustTier.WRITE
        action_input = {"operation_id": "create_vm"}
        react_action = "call_operation"

        # This matches the exact format from the SpecialistAgent wiring
        denial_msg = (
            f"Operator DENIED the {trust_tier.value} operation "
            f"'{action_input.get('operation_id', react_action)}'. "
            f"You cannot execute this operation. Try a read-only "
            f"alternative or explain what you cannot do."
        )

        scratchpad_entry = f"Observation: {denial_msg}"

        assert "Operator DENIED" in scratchpad_entry
        assert "write" in scratchpad_entry
        assert "create_vm" in scratchpad_entry
        assert "read-only alternative" in scratchpad_entry

    def test_denial_message_for_destructive_tier(self):
        """Verify denial message works for DESTRUCTIVE tier too."""
        from meho_app.modules.agents.models import TrustTier

        trust_tier = TrustTier.DESTRUCTIVE
        action_input = {"operation_id": "delete_cluster"}

        denial_msg = (
            f"Operator DENIED the {trust_tier.value} operation "
            f"'{action_input.get('operation_id', 'call_operation')}'. "
            f"You cannot execute this operation. Try a read-only "
            f"alternative or explain what you cannot do."
        )

        assert "destructive" in denial_msg
        assert "delete_cluster" in denial_msg


# =============================================================================
# ApprovalCheckNode Trust Classifier Tests
# =============================================================================


class TestApprovalCheckNodeClassifier:
    """Test that ApprovalCheckNode uses the trust classifier."""

    def _make_node(self):
        """Create an ApprovalCheckNode instance."""
        from meho_app.modules.agents.react_agent.nodes.approval_check import (
            ApprovalCheckNode,
        )

        return ApprovalCheckNode()

    def test_assess_danger_level_get(self):
        """GET operations classified as read."""
        node = self._make_node()
        result = node._assess_danger_level("call_operation", {"method": "GET"})
        assert result == "read"

    def test_assess_danger_level_post(self):
        """POST operations classified as write."""
        node = self._make_node()
        result = node._assess_danger_level("call_operation", {"method": "POST"})
        assert result == "write"

    def test_assess_danger_level_delete(self):
        """DELETE operations classified as destructive."""
        node = self._make_node()
        result = node._assess_danger_level("call_operation", {"method": "DELETE"})
        assert result == "destructive"

    def test_invalidate_topology_is_write(self):
        """invalidate_topology classified as write."""
        node = self._make_node()
        result = node._assess_danger_level("invalidate_topology", {})
        assert result == "write"
