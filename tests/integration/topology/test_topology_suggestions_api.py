# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for topology suggestion API endpoints.

Tests the full request/response flow for suggestion endpoints.
"""

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from meho_app.main import app
from meho_app.modules.topology.models import (
    TopologyEntityModel,
    TopologySameAsSuggestionModel,
)

# Mock user context for tests
MOCK_USER = {
    "user_id": "test-user-123",
    "tenant_id": "test-tenant",
    "roles": ["admin"],
    "email": "test@example.com",
}


@pytest.fixture
def mock_user_context():
    """Mock authenticated user context."""
    from meho_app.core.auth_context import UserContext

    return UserContext(
        user_id=MOCK_USER["user_id"],
        tenant_id=MOCK_USER["tenant_id"],
        roles=MOCK_USER["roles"],
        email=MOCK_USER["email"],
    )


@pytest.fixture
def mock_entities():
    """Create mock entities for testing."""
    entity_a = TopologyEntityModel(
        id=uuid4(),
        name="shop-ingress",
        description="K8s Ingress shop-ingress",
        connector_id=uuid4(),
        connector_name="Production K8s",
        raw_attributes={"kind": "Ingress"},
        discovered_at=datetime.now(tz=UTC),
        tenant_id="test-tenant",
    )
    entity_b = TopologyEntityModel(
        id=uuid4(),
        name="E-Commerce API",
        description="REST connector targeting shop.example.com",
        connector_id=uuid4(),
        connector_name=None,
        raw_attributes={"connector_type": "rest"},
        discovered_at=datetime.now(tz=UTC),
        tenant_id="test-tenant",
    )
    return entity_a, entity_b


@pytest.fixture
def mock_suggestion(mock_entities):
    """Create mock suggestion for testing."""
    entity_a, entity_b = mock_entities
    return TopologySameAsSuggestionModel(
        id=uuid4(),
        entity_a_id=entity_a.id,
        entity_b_id=entity_b.id,
        confidence=0.95,
        match_type="hostname_match",
        match_details="Entity 'shop-ingress' matches connector 'E-Commerce API'",
        status="pending",
        suggested_at=datetime.now(tz=UTC),
        tenant_id="test-tenant",
    )


class TestSuggestionsListEndpoint:
    """Tests for GET /api/topology/suggestions endpoint."""

    @pytest.mark.asyncio
    async def test_list_suggestions_empty(self, mock_user_context):
        """Test listing suggestions when none exist."""
        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_pending_suggestions"
            ) as mock_get:
                mock_get.return_value = ([], 0)

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get("/api/topology/suggestions")

                assert response.status_code == 200
                data = response.json()
                assert data["suggestions"] == []
                assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_suggestions_with_results(
        self, mock_user_context, mock_entities, mock_suggestion
    ):
        """Test listing suggestions with results."""
        entity_a, entity_b = mock_entities
        mock_suggestion.entity_a = entity_a
        mock_suggestion.entity_b = entity_b

        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_pending_suggestions"
            ) as mock_get:
                mock_get.return_value = ([mock_suggestion], 1)

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get("/api/topology/suggestions")

                assert response.status_code == 200
                data = response.json()
                assert data["total"] == 1
                assert len(data["suggestions"]) == 1

                suggestion = data["suggestions"][0]
                assert suggestion["entity_a_name"] == "shop-ingress"
                assert suggestion["entity_b_name"] == "E-Commerce API"
                assert suggestion["confidence"] == 0.95
                assert suggestion["match_type"] == "hostname_match"
                assert suggestion["status"] == "pending"


class TestSuggestionGetEndpoint:
    """Tests for GET /api/topology/suggestions/{id} endpoint."""

    @pytest.mark.asyncio
    async def test_get_suggestion_not_found(self, mock_user_context):
        """Test getting a suggestion that doesn't exist."""
        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestion_by_id"
            ) as mock_get:
                mock_get.return_value = None

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get(f"/api/topology/suggestions/{uuid4()}")

                assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_suggestion_wrong_tenant(
        self, mock_user_context, mock_entities, mock_suggestion
    ):
        """Test getting a suggestion from wrong tenant."""
        entity_a, entity_b = mock_entities
        mock_suggestion.entity_a = entity_a
        mock_suggestion.entity_b = entity_b
        mock_suggestion.tenant_id = "other-tenant"

        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestion_by_id"
            ) as mock_get:
                mock_get.return_value = mock_suggestion

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.get(f"/api/topology/suggestions/{mock_suggestion.id}")

                assert response.status_code == 403


class TestSuggestionApproveEndpoint:
    """Tests for POST /api/topology/suggestions/{id}/approve endpoint."""

    @pytest.mark.asyncio
    async def test_approve_suggestion_not_found(self, mock_user_context):
        """Test approving a suggestion that doesn't exist."""
        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestion_by_id"
            ) as mock_get:
                mock_get.return_value = None

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(f"/api/topology/suggestions/{uuid4()}/approve")

                assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_approve_already_resolved(
        self, mock_user_context, mock_entities, mock_suggestion
    ):
        """Test approving an already resolved suggestion."""
        entity_a, entity_b = mock_entities
        mock_suggestion.entity_a = entity_a
        mock_suggestion.entity_b = entity_b
        mock_suggestion.status = "approved"

        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestion_by_id"
            ) as mock_get:
                mock_get.return_value = mock_suggestion

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        f"/api/topology/suggestions/{mock_suggestion.id}/approve"
                    )

                assert response.status_code == 400
                assert "already" in response.json()["detail"].lower()


class TestSuggestionRejectEndpoint:
    """Tests for POST /api/topology/suggestions/{id}/reject endpoint."""

    @pytest.mark.asyncio
    async def test_reject_suggestion_not_found(self, mock_user_context):
        """Test rejecting a suggestion that doesn't exist."""
        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestion_by_id"
            ) as mock_get:
                mock_get.return_value = None

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(f"/api/topology/suggestions/{uuid4()}/reject")

                assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_reject_already_resolved(self, mock_user_context, mock_entities, mock_suggestion):
        """Test rejecting an already resolved suggestion."""
        entity_a, entity_b = mock_entities
        mock_suggestion.entity_a = entity_a
        mock_suggestion.entity_b = entity_b
        mock_suggestion.status = "rejected"

        with patch("meho_app.api.auth.get_current_user", return_value=mock_user_context):  # noqa: SIM117 -- readability preferred over combined with
            with patch(
                "meho_app.modules.topology.repository.TopologyRepository.get_suggestion_by_id"
            ) as mock_get:
                mock_get.return_value = mock_suggestion

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        f"/api/topology/suggestions/{mock_suggestion.id}/reject"
                    )

                assert response.status_code == 400
                assert "already" in response.json()["detail"].lower()
