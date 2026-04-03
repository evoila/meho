# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for SuggestionVerifier service.

Tests LLM-assisted verification of SAME_AS suggestions (TASK-144 Phase 3).
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from meho_app.modules.topology.correlation import LLMCorrelationResult
from meho_app.modules.topology.models import (
    TopologyEntityModel,
    TopologySameAsSuggestionModel,
)
from meho_app.modules.topology.suggestion_verifier import SuggestionVerifier


class TestSuggestionVerifierVerify:
    """Tests for verify_suggestion method."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        return MagicMock()

    @pytest.fixture
    def mock_entities(self):
        """Create mock entities for testing."""
        entity_a = TopologyEntityModel(
            id=uuid4(),
            name="shop-ingress",
            connector_id=uuid4(),
            connector_name="Kubernetes Cluster",
            description="K8s Ingress shop-ingress, ns default, hosts: api.shop.com",
            raw_attributes={"kind": "Ingress", "spec": {"rules": [{"host": "api.shop.com"}]}},
            tenant_id="test-tenant",
            discovered_at=datetime.now(tz=UTC),
        )

        entity_b = TopologyEntityModel(
            id=uuid4(),
            name="E-Commerce API Connector",
            connector_id=uuid4(),
            connector_name="REST Connector",
            description="REST connector targeting api.shop.com (E-Commerce API)",
            raw_attributes={
                "connector_type": "rest",
                "base_url": "https://api.shop.com/v1",
                "target_host": "api.shop.com",
            },
            tenant_id="test-tenant",
            discovered_at=datetime.now(tz=UTC),
        )

        return entity_a, entity_b

    @pytest.fixture
    def mock_suggestion(self, mock_entities):
        """Create a mock suggestion."""
        entity_a, entity_b = mock_entities
        return TopologySameAsSuggestionModel(
            id=uuid4(),
            entity_a_id=entity_a.id,
            entity_b_id=entity_b.id,
            entity_a=entity_a,
            entity_b=entity_b,
            confidence=0.85,
            match_type="hostname_match",
            match_details="Entity 'shop-ingress' hostname matches connector target",
            status="pending",
            tenant_id="test-tenant",
            suggested_at=datetime.now(tz=UTC),
            llm_verification_attempted=False,
            llm_verification_result=None,
        )

    @pytest.mark.asyncio
    async def test_verify_suggestion_calls_llm(self, mock_session, mock_entities, mock_suggestion):
        """Test that verify_suggestion calls the LLM correlation function."""
        entity_a, entity_b = mock_entities

        # Mock repository
        mock_repo = MagicMock()
        mock_repo.get_entity_by_id = AsyncMock(side_effect=[entity_a, entity_b])

        # Mock LLM result
        llm_result = LLMCorrelationResult(
            is_same_resource=True,
            confidence=0.95,
            reasoning="Both entities reference the same hostname api.shop.com",
            matching_identifiers=["hostname: api.shop.com"],
        )

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch(
                "meho_app.modules.topology.suggestion_verifier.confirm_same_as_with_llm",
                new_callable=AsyncMock,
                return_value=llm_result,
            ) as mock_llm,
        ):
            verifier = SuggestionVerifier(mock_session)
            result = await verifier.verify_suggestion(mock_suggestion)

            # Verify LLM was called with correct entities
            mock_llm.assert_called_once()
            call_kwargs = mock_llm.call_args[1]
            assert call_kwargs["entity_a"].name == "shop-ingress"
            assert call_kwargs["entity_b"].name == "E-Commerce API Connector"

            # Verify result
            assert result is not None
            assert result.is_same_resource is True
            assert result.confidence == pytest.approx(0.95)

    @pytest.mark.asyncio
    async def test_verify_suggestion_missing_entity(self, mock_session, mock_suggestion):
        """Test that verify_suggestion handles missing entities."""
        # Mock repository returning None for one entity
        mock_repo = MagicMock()
        mock_repo.get_entity_by_id = AsyncMock(side_effect=[None, MagicMock()])

        with patch(
            "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
            return_value=mock_repo,
        ):
            verifier = SuggestionVerifier(mock_session)
            result = await verifier.verify_suggestion(mock_suggestion)

            assert result is None

    @pytest.mark.asyncio
    async def test_verify_suggestion_llm_failure(
        self, mock_session, mock_entities, mock_suggestion
    ):
        """Test that verify_suggestion handles LLM failure gracefully."""
        entity_a, entity_b = mock_entities

        mock_repo = MagicMock()
        mock_repo.get_entity_by_id = AsyncMock(side_effect=[entity_a, entity_b])

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch(
                "meho_app.modules.topology.suggestion_verifier.confirm_same_as_with_llm",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            verifier = SuggestionVerifier(mock_session)
            result = await verifier.verify_suggestion(mock_suggestion)

            assert result is None


class TestSuggestionVerifierProcessAndResolve:
    """Tests for process_and_resolve method."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        return MagicMock()

    @pytest.fixture
    def mock_suggestion(self):
        """Create a basic mock suggestion."""
        entity_a = TopologyEntityModel(
            id=uuid4(),
            name="entity-a",
            connector_name="Connector A",
            description="Entity A",
            raw_attributes={},
            tenant_id="test-tenant",
            discovered_at=datetime.now(tz=UTC),
        )
        entity_b = TopologyEntityModel(
            id=uuid4(),
            name="entity-b",
            connector_name="Connector B",
            description="Entity B",
            raw_attributes={},
            tenant_id="test-tenant",
            discovered_at=datetime.now(tz=UTC),
        )

        return TopologySameAsSuggestionModel(
            id=uuid4(),
            entity_a_id=entity_a.id,
            entity_b_id=entity_b.id,
            entity_a=entity_a,
            entity_b=entity_b,
            confidence=0.85,
            match_type="ip_match",
            status="pending",
            tenant_id="test-tenant",
            suggested_at=datetime.now(tz=UTC),
            llm_verification_attempted=False,
            llm_verification_result=None,
        )

    @pytest.mark.asyncio
    async def test_approve_when_llm_confident_same(self, mock_session, mock_suggestion):
        """Test auto-approve when LLM confidently says entities are the same."""
        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=mock_suggestion)
        mock_repo.get_entity_by_id = AsyncMock(
            side_effect=[
                mock_suggestion.entity_a,
                mock_suggestion.entity_b,
            ]
        )
        mock_repo.update_suggestion_verification = AsyncMock()
        mock_repo.approve_suggestion = AsyncMock()

        llm_result = LLMCorrelationResult(
            is_same_resource=True,
            confidence=0.92,  # Above threshold
            reasoning="These are the same resource",
            matching_identifiers=["IP: 10.0.0.5"],
        )

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch(
                "meho_app.modules.topology.suggestion_verifier.confirm_same_as_with_llm",
                new_callable=AsyncMock,
                return_value=llm_result,
            ),
            patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config,
        ):
            mock_config.return_value.suggestion_llm_approve_confidence = 0.80

            verifier = SuggestionVerifier(mock_session)
            status = await verifier.process_and_resolve(mock_suggestion.id)

            assert status == "approved"
            mock_repo.approve_suggestion.assert_called_once_with(
                suggestion_id=mock_suggestion.id,
                user_id="llm_verification",
            )

    @pytest.mark.asyncio
    async def test_reject_when_llm_confident_different(self, mock_session, mock_suggestion):
        """Test auto-reject when LLM confidently says entities are different."""
        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=mock_suggestion)
        mock_repo.get_entity_by_id = AsyncMock(
            side_effect=[
                mock_suggestion.entity_a,
                mock_suggestion.entity_b,
            ]
        )
        mock_repo.update_suggestion_verification = AsyncMock()
        mock_repo.reject_suggestion = AsyncMock()

        llm_result = LLMCorrelationResult(
            is_same_resource=False,
            confidence=0.88,  # Above threshold
            reasoning="These are different resources",
            matching_identifiers=[],
        )

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch(
                "meho_app.modules.topology.suggestion_verifier.confirm_same_as_with_llm",
                new_callable=AsyncMock,
                return_value=llm_result,
            ),
            patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config,
        ):
            mock_config.return_value.suggestion_llm_approve_confidence = 0.80

            verifier = SuggestionVerifier(mock_session)
            status = await verifier.process_and_resolve(mock_suggestion.id)

            assert status == "rejected"
            mock_repo.reject_suggestion.assert_called_once()

    @pytest.mark.asyncio
    async def test_pending_when_llm_uncertain(self, mock_session, mock_suggestion):
        """Test leave pending when LLM is uncertain."""
        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=mock_suggestion)
        mock_repo.get_entity_by_id = AsyncMock(
            side_effect=[
                mock_suggestion.entity_a,
                mock_suggestion.entity_b,
            ]
        )
        mock_repo.update_suggestion_verification = AsyncMock()

        llm_result = LLMCorrelationResult(
            is_same_resource=True,
            confidence=0.55,  # Below threshold
            reasoning="Not enough evidence",
            matching_identifiers=[],
        )

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch(
                "meho_app.modules.topology.suggestion_verifier.confirm_same_as_with_llm",
                new_callable=AsyncMock,
                return_value=llm_result,
            ),
            patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config,
        ):
            mock_config.return_value.suggestion_llm_approve_confidence = 0.80

            verifier = SuggestionVerifier(mock_session)
            status = await verifier.process_and_resolve(mock_suggestion.id)

            assert status == "pending"
            # Verification was stored but no approval/rejection
            mock_repo.update_suggestion_verification.assert_called_once()

    @pytest.mark.asyncio
    async def test_pending_when_llm_fails(self, mock_session, mock_suggestion):
        """Test leave pending when LLM verification fails."""
        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=mock_suggestion)
        mock_repo.get_entity_by_id = AsyncMock(
            side_effect=[
                mock_suggestion.entity_a,
                mock_suggestion.entity_b,
            ]
        )
        mock_repo.update_suggestion_verification = AsyncMock()

        with (  # noqa: SIM117 -- readability preferred over combined with
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch(
                "meho_app.modules.topology.suggestion_verifier.confirm_same_as_with_llm",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):  # LLM failed
            with patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config:
                mock_config.return_value.suggestion_llm_approve_confidence = 0.80

                verifier = SuggestionVerifier(mock_session)
                status = await verifier.process_and_resolve(mock_suggestion.id)

                assert status == "pending"

    @pytest.mark.asyncio
    async def test_skip_already_resolved_suggestion(self, mock_session, mock_suggestion):
        """Test that already resolved suggestions are skipped."""
        mock_suggestion.status = "approved"  # Already resolved

        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=mock_suggestion)

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config,
        ):
            mock_config.return_value.suggestion_llm_approve_confidence = 0.80

            verifier = SuggestionVerifier(mock_session)
            status = await verifier.process_and_resolve(mock_suggestion.id)

            assert status == "approved"  # Returns existing status

    @pytest.mark.asyncio
    async def test_skip_already_verified_suggestion(self, mock_session, mock_suggestion):
        """Test that already LLM-verified suggestions are skipped."""
        mock_suggestion.llm_verification_attempted = True

        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=mock_suggestion)

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config,
        ):
            mock_config.return_value.suggestion_llm_approve_confidence = 0.80

            verifier = SuggestionVerifier(mock_session)
            status = await verifier.process_and_resolve(mock_suggestion.id)

            assert status == "pending"  # Returns existing status

    @pytest.mark.asyncio
    async def test_suggestion_not_found(self, mock_session):
        """Test handling when suggestion is not found."""
        mock_repo = MagicMock()
        mock_repo.get_suggestion_by_id = AsyncMock(return_value=None)

        with (
            patch(
                "meho_app.modules.topology.suggestion_verifier.TopologyRepository",
                return_value=mock_repo,
            ),
            patch("meho_app.modules.topology.suggestion_verifier.get_config") as mock_config,
        ):
            mock_config.return_value.suggestion_llm_approve_confidence = 0.80

            verifier = SuggestionVerifier(mock_session)
            status = await verifier.process_and_resolve(uuid4())

            assert status == "pending"


class TestLLMCorrelationResultSchema:
    """Tests for LLMCorrelationResult schema."""

    def test_valid_result(self):
        """Test creating a valid result."""
        result = LLMCorrelationResult(
            is_same_resource=True,
            confidence=0.95,
            reasoning="Both entities reference the same hostname",
            matching_identifiers=["hostname: api.example.com", "IP: 10.0.0.5"],
        )

        assert result.is_same_resource is True
        assert result.confidence == pytest.approx(0.95)
        assert "hostname" in result.reasoning
        assert len(result.matching_identifiers) == 2

    def test_model_dump(self):
        """Test that result can be serialized to dict."""
        result = LLMCorrelationResult(
            is_same_resource=False,
            confidence=0.88,
            reasoning="No matching identifiers found",
            matching_identifiers=[],
        )

        data = result.model_dump()

        assert data["is_same_resource"] is False
        assert data["confidence"] == pytest.approx(0.88)
        assert isinstance(data["matching_identifiers"], list)

    def test_confidence_bounds(self):
        """Test confidence is validated within bounds."""
        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            LLMCorrelationResult(
                is_same_resource=True,
                confidence=1.5,  # Invalid: > 1.0
                reasoning="Test",
                matching_identifiers=[],
            )

        with pytest.raises(ValueError):  # noqa: PT011 -- test validates exception type is sufficient
            LLMCorrelationResult(
                is_same_resource=True,
                confidence=-0.1,  # Invalid: < 0.0
                reasoning="Test",
                matching_identifiers=[],
            )
