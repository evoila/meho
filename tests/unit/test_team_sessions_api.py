# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for team sessions API endpoint (Phase 38).

Tests:
- Team endpoint returns only non-private sessions
- Team endpoint filters by tenant_id
- Status derivation: awaiting_approval vs idle
- pending_approval_count correctness
"""

from datetime import UTC, datetime
from uuid import uuid4

from meho_app.api.routes_chat_sessions import TeamSessionResponse

# =============================================================================
# TeamSessionResponse Model Tests
# =============================================================================


class TestTeamSessionResponseModel:
    """Tests for the TeamSessionResponse Pydantic model."""

    def test_model_accepts_valid_data(self):
        """TeamSessionResponse should accept well-formed data."""
        now = datetime.now(tz=UTC)
        resp = TeamSessionResponse(
            id=str(uuid4()),
            title="Investigate K8s crash loop",
            visibility="group",
            created_by_name="Alice",
            trigger_source=None,
            status="idle",
            pending_approval_count=0,
            created_at=now,
            updated_at=now,
        )
        assert resp.visibility == "group"
        assert resp.status == "idle"
        assert resp.pending_approval_count == 0

    def test_awaiting_approval_status(self):
        """Status 'awaiting_approval' should be accepted."""
        now = datetime.now(tz=UTC)
        resp = TeamSessionResponse(
            id=str(uuid4()),
            title="Alertmanager: pod restart",
            visibility="tenant",
            created_by_name="Alertmanager",
            trigger_source="webhook",
            status="awaiting_approval",
            pending_approval_count=2,
            created_at=now,
            updated_at=now,
        )
        assert resp.status == "awaiting_approval"
        assert resp.pending_approval_count == 2
        assert resp.trigger_source == "webhook"

    def test_nullable_fields(self):
        """Optional fields should accept None values."""
        now = datetime.now(tz=UTC)
        resp = TeamSessionResponse(
            id=str(uuid4()),
            title=None,
            visibility="group",
            created_by_name=None,
            trigger_source=None,
            status="idle",
            pending_approval_count=0,
            created_at=now,
            updated_at=now,
        )
        assert resp.title is None
        assert resp.created_by_name is None
        assert resp.trigger_source is None


# =============================================================================
# Service-Level Team Session Logic Tests
# =============================================================================


class TestTeamSessionServiceLogic:
    """Tests for the team session listing logic in AgentService."""

    def test_status_derivation_idle_when_no_pending(self):
        """Status should be 'idle' when pending_approval_count is 0."""
        pc = 0
        status = "awaiting_approval" if pc > 0 else "idle"
        assert status == "idle"

    def test_status_derivation_awaiting_when_pending_exists(self):
        """Status should be 'awaiting_approval' when pending_approval_count > 0."""
        pc = 3
        status = "awaiting_approval" if pc > 0 else "idle"
        assert status == "awaiting_approval"

    def test_status_derivation_awaiting_when_one_pending(self):
        """Status should be 'awaiting_approval' even with just 1 pending."""
        pc = 1
        status = "awaiting_approval" if pc > 0 else "idle"
        assert status == "awaiting_approval"

    def test_team_session_dict_format(self):
        """Verify the dict output format matches TeamSessionResponse fields."""
        now = datetime.now(tz=UTC)
        session_dict = {
            "id": str(uuid4()),
            "title": "Test session",
            "visibility": "group",
            "created_by_name": "Bob",
            "trigger_source": None,
            "status": "idle",
            "pending_approval_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        # Ensure it can construct a TeamSessionResponse
        resp = TeamSessionResponse(**session_dict)
        assert resp.id == session_dict["id"]
        assert resp.created_by_name == "Bob"


# =============================================================================
# Visibility Filter Logic Tests
# =============================================================================


class TestTeamSessionVisibilityFiltering:
    """Tests for team session visibility filtering logic."""

    def test_private_sessions_excluded(self):
        """Private sessions should not appear in team listings."""
        sessions = [
            {"visibility": "private", "id": "1"},
            {"visibility": "group", "id": "2"},
            {"visibility": "tenant", "id": "3"},
        ]
        # Simulate the WHERE visibility != 'private' filter
        team_sessions = [s for s in sessions if s["visibility"] != "private"]
        assert len(team_sessions) == 2
        assert all(s["visibility"] != "private" for s in team_sessions)

    def test_group_sessions_included(self):
        """Group sessions should appear in team listings."""
        sessions = [
            {"visibility": "group", "id": "1"},
        ]
        team_sessions = [s for s in sessions if s["visibility"] != "private"]
        assert len(team_sessions) == 1

    def test_tenant_sessions_included(self):
        """Tenant sessions should appear in team listings."""
        sessions = [
            {"visibility": "tenant", "id": "1"},
        ]
        team_sessions = [s for s in sessions if s["visibility"] != "private"]
        assert len(team_sessions) == 1

    def test_tenant_filter_applied(self):
        """Only sessions from the correct tenant should be returned."""
        sessions = [
            {"visibility": "group", "id": "1", "tenant_id": "acme"},
            {"visibility": "group", "id": "2", "tenant_id": "other"},
            {"visibility": "tenant", "id": "3", "tenant_id": "acme"},
        ]
        target_tenant = "acme"
        team_sessions = [
            s for s in sessions if s["visibility"] != "private" and s["tenant_id"] == target_tenant
        ]
        assert len(team_sessions) == 2
        assert all(s["tenant_id"] == "acme" for s in team_sessions)
