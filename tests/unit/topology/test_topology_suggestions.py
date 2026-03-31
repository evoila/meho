# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for topology suggestion schemas and models.

Tests SAME_AS suggestion schema validation.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from meho_app.modules.topology.schemas import (
    SameAsSuggestion,
    SameAsSuggestionCreate,
    SameAsSuggestionWithEntities,
    SuggestionActionResponse,
    SuggestionListResponse,
    SuggestionMatchType,
    SuggestionStatus,
)


class TestSuggestionSchemas:
    """Tests for suggestion Pydantic schemas."""

    # =========================================================================
    # SameAsSuggestionCreate tests
    # =========================================================================

    def test_create_suggestion_valid(self):
        """Test creating a valid suggestion."""
        suggestion = SameAsSuggestionCreate(
            entity_a_id=uuid4(),
            entity_b_id=uuid4(),
            confidence=0.95,
            match_type="hostname_match",
            match_details="Entity 'shop-ingress' matches connector 'E-Commerce API'",
        )

        assert suggestion.confidence == 0.95
        assert suggestion.match_type == "hostname_match"
        assert suggestion.match_details is not None

    def test_create_suggestion_without_details(self):
        """Test creating suggestion without match_details."""
        suggestion = SameAsSuggestionCreate(
            entity_a_id=uuid4(),
            entity_b_id=uuid4(),
            confidence=0.90,
            match_type="ip_match",
        )

        assert suggestion.match_details is None

    def test_create_suggestion_invalid_confidence_too_low(self):
        """Test that confidence < 0 raises error."""
        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            SameAsSuggestionCreate(
                entity_a_id=uuid4(),
                entity_b_id=uuid4(),
                confidence=-0.1,
                match_type="hostname_match",
            )

    def test_create_suggestion_invalid_confidence_too_high(self):
        """Test that confidence > 1 raises error."""
        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            SameAsSuggestionCreate(
                entity_a_id=uuid4(),
                entity_b_id=uuid4(),
                confidence=1.5,
                match_type="hostname_match",
            )

    # =========================================================================
    # SameAsSuggestion tests
    # =========================================================================

    def test_suggestion_full_schema(self):
        """Test full suggestion schema with all fields."""
        now = datetime.now(tz=UTC)
        suggestion = SameAsSuggestion(
            id=uuid4(),
            entity_a_id=uuid4(),
            entity_b_id=uuid4(),
            confidence=0.95,
            match_type="hostname_match",
            match_details="Test match",
            status="pending",
            suggested_at=now,
            resolved_at=None,
            resolved_by=None,
            tenant_id="test-tenant",
        )

        assert suggestion.status == "pending"
        assert suggestion.resolved_at is None

    def test_suggestion_resolved(self):
        """Test suggestion that has been resolved."""
        now = datetime.now(tz=UTC)
        suggestion = SameAsSuggestion(
            id=uuid4(),
            entity_a_id=uuid4(),
            entity_b_id=uuid4(),
            confidence=0.95,
            match_type="hostname_match",
            status="approved",
            suggested_at=now,
            resolved_at=now,
            resolved_by="user-123",
            tenant_id="test-tenant",
        )

        assert suggestion.status == "approved"
        assert suggestion.resolved_by == "user-123"

    # =========================================================================
    # SameAsSuggestionWithEntities tests
    # =========================================================================

    def test_suggestion_with_entities(self):
        """Test suggestion with entity names included."""
        now = datetime.now(tz=UTC)
        suggestion = SameAsSuggestionWithEntities(
            id=uuid4(),
            entity_a_id=uuid4(),
            entity_b_id=uuid4(),
            confidence=0.95,
            match_type="hostname_match",
            status="pending",
            suggested_at=now,
            tenant_id="test-tenant",
            entity_a_name="shop-ingress",
            entity_b_name="E-Commerce API",
            entity_a_connector_name="Production K8s",
            entity_b_connector_name=None,
        )

        assert suggestion.entity_a_name == "shop-ingress"
        assert suggestion.entity_b_name == "E-Commerce API"
        assert suggestion.entity_a_connector_name == "Production K8s"
        assert suggestion.entity_b_connector_name is None

    # =========================================================================
    # SuggestionListResponse tests
    # =========================================================================

    def test_suggestion_list_response(self):
        """Test suggestion list response."""
        now = datetime.now(tz=UTC)
        suggestions = [
            SameAsSuggestionWithEntities(
                id=uuid4(),
                entity_a_id=uuid4(),
                entity_b_id=uuid4(),
                confidence=0.95,
                match_type="hostname_match",
                status="pending",
                suggested_at=now,
                tenant_id="test-tenant",
                entity_a_name="entity-1",
                entity_b_name="entity-2",
            ),
            SameAsSuggestionWithEntities(
                id=uuid4(),
                entity_a_id=uuid4(),
                entity_b_id=uuid4(),
                confidence=0.90,
                match_type="ip_match",
                status="pending",
                suggested_at=now,
                tenant_id="test-tenant",
                entity_a_name="entity-3",
                entity_b_name="entity-4",
            ),
        ]

        response = SuggestionListResponse(
            suggestions=suggestions,
            total=10,
        )

        assert len(response.suggestions) == 2
        assert response.total == 10

    # =========================================================================
    # SuggestionActionResponse tests
    # =========================================================================

    def test_approve_action_response(self):
        """Test approve action response."""
        response = SuggestionActionResponse(
            success=True,
            message="Created SAME_AS relationship",
            same_as_created=True,
        )

        assert response.success is True
        assert response.same_as_created is True

    def test_reject_action_response(self):
        """Test reject action response."""
        response = SuggestionActionResponse(
            success=True,
            message="Suggestion rejected",
            same_as_created=False,
        )

        assert response.success is True
        assert response.same_as_created is False


class TestSuggestionEnums:
    """Tests for suggestion enums."""

    def test_suggestion_status_values(self):
        """Test SuggestionStatus enum values."""
        assert SuggestionStatus.PENDING.value == "pending"
        assert SuggestionStatus.APPROVED.value == "approved"
        assert SuggestionStatus.REJECTED.value == "rejected"

    def test_suggestion_match_type_values(self):
        """Test SuggestionMatchType enum values."""
        assert SuggestionMatchType.HOSTNAME_MATCH.value == "hostname_match"
        assert SuggestionMatchType.IP_MATCH.value == "ip_match"
        assert SuggestionMatchType.PARTIAL_HOSTNAME.value == "partial_hostname"
