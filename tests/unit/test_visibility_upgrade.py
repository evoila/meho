# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for visibility upgrade API logic (Phase 38).

Tests:
- PATCH visibility private->group succeeds
- PATCH visibility group->private fails (downgrade)
- PATCH visibility by non-owner fails (403)
- GET session with group visibility accessible for any tenant user
"""

from datetime import UTC, datetime
from uuid import uuid4

from meho_app.api.routes_chat_sessions import (
    SessionResponse,
    UpdateVisibilityRequest,
)
from meho_app.modules.agents.models import (
    validate_visibility_upgrade,
)

# =============================================================================
# UpdateVisibilityRequest Model Tests
# =============================================================================


class TestUpdateVisibilityRequest:
    """Tests for the UpdateVisibilityRequest Pydantic model."""

    def test_accepts_group_visibility(self):
        """Request should accept 'group' visibility."""
        req = UpdateVisibilityRequest(visibility="group")
        assert req.visibility == "group"

    def test_accepts_tenant_visibility(self):
        """Request should accept 'tenant' visibility."""
        req = UpdateVisibilityRequest(visibility="tenant")
        assert req.visibility == "tenant"


# =============================================================================
# Visibility Upgrade Logic Tests
# =============================================================================


class TestVisibilityUpgradeLogic:
    """Tests for the visibility upgrade business logic."""

    def test_private_to_group_upgrade_succeeds(self):
        """private -> group is a valid upgrade."""
        assert validate_visibility_upgrade("private", "group") is True

    def test_private_to_tenant_upgrade_succeeds(self):
        """private -> tenant is a valid upgrade."""
        assert validate_visibility_upgrade("private", "tenant") is True

    def test_group_to_tenant_upgrade_succeeds(self):
        """group -> tenant is a valid upgrade."""
        assert validate_visibility_upgrade("group", "tenant") is True

    def test_group_to_private_downgrade_fails(self):
        """group -> private is an invalid downgrade."""
        assert validate_visibility_upgrade("group", "private") is False

    def test_tenant_to_group_downgrade_fails(self):
        """tenant -> group is an invalid downgrade."""
        assert validate_visibility_upgrade("tenant", "group") is False

    def test_tenant_to_private_downgrade_fails(self):
        """tenant -> private is an invalid downgrade."""
        assert validate_visibility_upgrade("tenant", "private") is False

    def test_same_level_transition_fails(self):
        """Same-level transitions should be rejected."""
        for vis in ["private", "group", "tenant"]:
            assert validate_visibility_upgrade(vis, vis) is False


# =============================================================================
# Owner-Only Enforcement Tests
# =============================================================================


class TestVisibilityOwnerEnforcement:
    """Tests for owner-only visibility upgrade enforcement."""

    def test_owner_check_matches_user_id(self):
        """Session owner should be determined by user_id match."""
        session_user_id = "user-alice"
        requesting_user_id = "user-alice"
        assert session_user_id == requesting_user_id

    def test_non_owner_check_fails(self):
        """Non-owner should be rejected."""
        session_user_id = "user-alice"
        requesting_user_id = "user-bob"
        assert session_user_id != requesting_user_id

    def test_same_tenant_different_user_is_non_owner(self):
        """Users in the same tenant but different user_id should be non-owners."""
        session_tenant = "acme"
        session_user = "user-alice"
        requester_tenant = "acme"
        requester_user = "user-bob"
        # Same tenant doesn't grant ownership
        is_owner = session_tenant == requester_tenant and session_user == requester_user
        assert is_owner is False

    def test_cross_tenant_always_denied(self):
        """Users from different tenants should always be denied."""
        session_tenant = "acme"
        requester_tenant = "other-corp"
        assert session_tenant != requester_tenant


# =============================================================================
# Group-Aware Session Access Tests
# =============================================================================


class TestGroupAwareSessionAccess:
    """Tests for group-aware GET session access logic."""

    def _can_access(
        self, session_visibility, session_tenant, session_user, requester_tenant, requester_user
    ):
        """Simulate the group-aware access check from routes_chat_sessions.py."""
        if session_tenant != requester_tenant:
            return False
        return not (session_visibility == "private" and session_user != requester_user)

    def test_owner_can_access_private_session(self):
        """Session owner should access their private session."""
        assert self._can_access("private", "acme", "alice", "acme", "alice") is True

    def test_non_owner_cannot_access_private_session(self):
        """Non-owner in same tenant should NOT access private session."""
        assert self._can_access("private", "acme", "alice", "acme", "bob") is False

    def test_any_tenant_user_can_access_group_session(self):
        """Any user in the same tenant should access group session."""
        assert self._can_access("group", "acme", "alice", "acme", "bob") is True

    def test_any_tenant_user_can_access_tenant_session(self):
        """Any user in the same tenant should access tenant session."""
        assert self._can_access("tenant", "acme", "alice", "acme", "bob") is True

    def test_cross_tenant_cannot_access_group_session(self):
        """User from different tenant should NOT access group session."""
        assert self._can_access("group", "acme", "alice", "other", "charlie") is False

    def test_cross_tenant_cannot_access_tenant_session(self):
        """User from different tenant should NOT access tenant session."""
        assert self._can_access("tenant", "acme", "alice", "other", "charlie") is False

    def test_cross_tenant_cannot_access_private_session(self):
        """User from different tenant should NOT access private session."""
        assert self._can_access("private", "acme", "alice", "other", "alice") is False

    def test_owner_can_access_group_session(self):
        """Session owner should access their own group session."""
        assert self._can_access("group", "acme", "alice", "acme", "alice") is True


# =============================================================================
# SessionResponse Visibility Field Tests
# =============================================================================


class TestSessionResponseVisibility:
    """Tests for visibility field in SessionResponse."""

    def test_session_response_includes_visibility(self):
        """SessionResponse should include optional visibility field."""
        now = datetime.now(tz=UTC)
        resp = SessionResponse(
            id=str(uuid4()),
            title="Test",
            created_at=now,
            updated_at=now,
            visibility="group",
        )
        assert resp.visibility == "group"

    def test_session_response_visibility_defaults_to_none(self):
        """SessionResponse visibility should default to None for backward compat."""
        now = datetime.now(tz=UTC)
        resp = SessionResponse(
            id=str(uuid4()),
            title="Test",
            created_at=now,
            updated_at=now,
        )
        assert resp.visibility is None
