# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for session visibility model and validation (Phase 38).

Tests:
- SessionVisibility enum values
- VISIBILITY_ORDER ordering
- validate_visibility_upgrade() upgrade-only enforcement
- ChatSessionModel visibility column default
"""

from meho_app.modules.agents.models import (
    VISIBILITY_ORDER,
    ChatSessionModel,
    SessionVisibility,
    validate_visibility_upgrade,
)

# =============================================================================
# SessionVisibility Enum Tests
# =============================================================================


class TestSessionVisibilityEnum:
    """Tests for the SessionVisibility enum."""

    def test_has_private_value(self):
        """SessionVisibility should have a PRIVATE member with value 'private'."""
        assert SessionVisibility.PRIVATE == "private"
        assert SessionVisibility.PRIVATE.value == "private"

    def test_has_group_value(self):
        """SessionVisibility should have a GROUP member with value 'group'."""
        assert SessionVisibility.GROUP == "group"
        assert SessionVisibility.GROUP.value == "group"

    def test_has_tenant_value(self):
        """SessionVisibility should have a TENANT member with value 'tenant'."""
        assert SessionVisibility.TENANT == "tenant"
        assert SessionVisibility.TENANT.value == "tenant"

    def test_exactly_three_members(self):
        """SessionVisibility should have exactly three members."""
        assert len(SessionVisibility) == 3

    def test_is_string_enum(self):
        """SessionVisibility values should be usable as strings."""
        assert isinstance(SessionVisibility.PRIVATE, str)
        assert isinstance(SessionVisibility.GROUP, str)
        assert isinstance(SessionVisibility.TENANT, str)

    def test_string_comparison(self):
        """SessionVisibility members should compare equal to their string values."""
        assert SessionVisibility.PRIVATE == "private"
        assert SessionVisibility.GROUP == "group"
        assert SessionVisibility.TENANT == "tenant"


# =============================================================================
# VISIBILITY_ORDER Tests
# =============================================================================


class TestVisibilityOrder:
    """Tests for the VISIBILITY_ORDER mapping."""

    def test_private_is_lowest(self):
        """PRIVATE should have the lowest ordering value (0)."""
        assert VISIBILITY_ORDER[SessionVisibility.PRIVATE] == 0

    def test_group_is_middle(self):
        """GROUP should have the middle ordering value (1)."""
        assert VISIBILITY_ORDER[SessionVisibility.GROUP] == 1

    def test_tenant_is_highest(self):
        """TENANT should have the highest ordering value (2)."""
        assert VISIBILITY_ORDER[SessionVisibility.TENANT] == 2

    def test_ordering_is_strictly_increasing(self):
        """Order values should be strictly increasing: private < group < tenant."""
        assert (
            VISIBILITY_ORDER[SessionVisibility.PRIVATE]
            < VISIBILITY_ORDER[SessionVisibility.GROUP]
            < VISIBILITY_ORDER[SessionVisibility.TENANT]
        )

    def test_all_enum_values_have_order(self):
        """Every SessionVisibility member should have an entry in VISIBILITY_ORDER."""
        for member in SessionVisibility:
            assert member in VISIBILITY_ORDER, f"{member} missing from VISIBILITY_ORDER"


# =============================================================================
# validate_visibility_upgrade() Tests
# =============================================================================


class TestValidateVisibilityUpgrade:
    """Tests for the upgrade-only enforcement function."""

    def test_private_to_group_is_valid(self):
        """Upgrading from private to group should be allowed."""
        assert validate_visibility_upgrade("private", "group") is True

    def test_private_to_tenant_is_valid(self):
        """Upgrading from private to tenant should be allowed."""
        assert validate_visibility_upgrade("private", "tenant") is True

    def test_group_to_tenant_is_valid(self):
        """Upgrading from group to tenant should be allowed."""
        assert validate_visibility_upgrade("group", "tenant") is True

    def test_group_to_private_is_invalid(self):
        """Downgrading from group to private should be rejected."""
        assert validate_visibility_upgrade("group", "private") is False

    def test_tenant_to_group_is_invalid(self):
        """Downgrading from tenant to group should be rejected."""
        assert validate_visibility_upgrade("tenant", "group") is False

    def test_tenant_to_private_is_invalid(self):
        """Downgrading from tenant to private should be rejected."""
        assert validate_visibility_upgrade("tenant", "private") is False

    def test_same_to_same_is_invalid(self):
        """Same-level transitions should be rejected (not strictly increasing)."""
        assert validate_visibility_upgrade("private", "private") is False
        assert validate_visibility_upgrade("group", "group") is False
        assert validate_visibility_upgrade("tenant", "tenant") is False

    def test_unknown_current_returns_false(self):
        """Unknown current visibility should return False."""
        assert validate_visibility_upgrade("unknown", "group") is False

    def test_unknown_requested_returns_false(self):
        """Unknown requested visibility should return False."""
        assert validate_visibility_upgrade("private", "unknown") is False


# =============================================================================
# ChatSessionModel Column Tests
# =============================================================================


class TestChatSessionModelVisibility:
    """Tests for ChatSessionModel visibility-related columns."""

    def test_has_visibility_column(self):
        """ChatSessionModel should have a visibility column."""
        assert hasattr(ChatSessionModel, "visibility")

    def test_visibility_server_default(self):
        """Visibility column should have server_default='private'."""
        col = ChatSessionModel.__table__.columns["visibility"]
        assert col.server_default is not None
        assert str(col.server_default.arg) == "private"

    def test_visibility_not_nullable(self):
        """Visibility column should not be nullable."""
        col = ChatSessionModel.__table__.columns["visibility"]
        assert col.nullable is False

    def test_has_created_by_name_column(self):
        """ChatSessionModel should have a created_by_name column."""
        assert hasattr(ChatSessionModel, "created_by_name")

    def test_created_by_name_is_nullable(self):
        """created_by_name should be nullable (null for programmatic sessions without names)."""
        col = ChatSessionModel.__table__.columns["created_by_name"]
        assert col.nullable is True

    def test_has_trigger_source_column(self):
        """ChatSessionModel should have a trigger_source column."""
        assert hasattr(ChatSessionModel, "trigger_source")

    def test_trigger_source_is_nullable(self):
        """trigger_source should be nullable (null for human-created sessions)."""
        col = ChatSessionModel.__table__.columns["trigger_source"]
        assert col.nullable is True
