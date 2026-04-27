# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for group-aware approval authorization (Phase 38).

Tests:
- Approval in private session by owner succeeds
- Approval in private session by non-owner fails
- Approval in group session by any tenant user succeeds
- Approval in group session by cross-tenant user fails
"""


# =============================================================================
# Group-Aware Approval Authorization Logic Tests
# =============================================================================


def can_approve_in_session(
    session_visibility: str,
    session_tenant_id: str,
    session_user_id: str,
    user_tenant_id: str,
    user_id: str,
) -> tuple[bool, str]:
    """Simulate group-aware approval authorization check from routes_chat.py.

    Returns (allowed, reason) tuple.
    """
    # Cross-tenant check
    if session_tenant_id != user_tenant_id:
        return False, "Access denied"

    # Private sessions: owner only
    if session_visibility == "private" and session_user_id != user_id:
        return False, "Only the session owner can approve in private sessions"

    # Group/tenant sessions: any tenant user
    return True, "ok"


class TestApprovalInPrivateSession:
    """Tests for approval authorization in private sessions."""

    def test_owner_can_approve_private(self):
        """Session owner should be able to approve in private sessions."""
        allowed, _ = can_approve_in_session(
            session_visibility="private",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="alice",
        )
        assert allowed is True

    def test_non_owner_cannot_approve_private(self):
        """Non-owner in same tenant should NOT approve in private sessions."""
        allowed, reason = can_approve_in_session(
            session_visibility="private",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="bob",
        )
        assert allowed is False
        assert "owner" in reason.lower()

    def test_cross_tenant_cannot_approve_private(self):
        """User from different tenant should NOT approve in private sessions."""
        allowed, reason = can_approve_in_session(
            session_visibility="private",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="other-corp",
            user_id="charlie",
        )
        assert allowed is False
        assert "access denied" in reason.lower()


class TestApprovalInGroupSession:
    """Tests for approval authorization in group sessions."""

    def test_owner_can_approve_group(self):
        """Session owner should be able to approve in group sessions."""
        allowed, _ = can_approve_in_session(
            session_visibility="group",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="alice",
        )
        assert allowed is True

    def test_any_tenant_user_can_approve_group(self):
        """Any user in the same tenant should approve in group sessions."""
        allowed, _ = can_approve_in_session(
            session_visibility="group",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="bob",
        )
        assert allowed is True

    def test_cross_tenant_cannot_approve_group(self):
        """User from different tenant should NOT approve in group sessions."""
        allowed, reason = can_approve_in_session(
            session_visibility="group",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="other-corp",
            user_id="charlie",
        )
        assert allowed is False
        assert "access denied" in reason.lower()


class TestApprovalInTenantSession:
    """Tests for approval authorization in tenant-wide sessions."""

    def test_owner_can_approve_tenant(self):
        """Session owner should approve in tenant sessions."""
        allowed, _ = can_approve_in_session(
            session_visibility="tenant",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="alice",
        )
        assert allowed is True

    def test_any_tenant_user_can_approve_tenant(self):
        """Any tenant user should approve in tenant sessions."""
        allowed, _ = can_approve_in_session(
            session_visibility="tenant",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="bob",
        )
        assert allowed is True

    def test_cross_tenant_cannot_approve_tenant(self):
        """Cross-tenant user should NOT approve in tenant sessions."""
        allowed, _ = can_approve_in_session(
            session_visibility="tenant",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="other-corp",
            user_id="charlie",
        )
        assert allowed is False


class TestApprovalEdgeCases:
    """Tests for edge cases in approval authorization."""

    def test_multiple_team_members_can_all_approve(self):
        """Multiple different users from the same tenant should all be able to approve."""
        for user_id in ["bob", "charlie", "diana", "eve"]:
            allowed, _ = can_approve_in_session(
                session_visibility="group",
                session_tenant_id="acme",
                session_user_id="alice",
                user_tenant_id="acme",
                user_id=user_id,
            )
            assert allowed is True, f"User {user_id} should be able to approve"

    def test_private_restricts_even_admins(self):
        """Private session authorization is user_id based, not role based."""
        # Admin in same tenant but different user_id
        allowed, _ = can_approve_in_session(
            session_visibility="private",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="acme",
            user_id="admin-bob",
        )
        assert allowed is False

    def test_owner_with_matching_user_id_in_different_tenant(self):
        """Same user_id in different tenant should still be denied."""
        allowed, _ = can_approve_in_session(
            session_visibility="group",
            session_tenant_id="acme",
            session_user_id="alice",
            user_tenant_id="evil-corp",
            user_id="alice",
        )
        assert allowed is False
