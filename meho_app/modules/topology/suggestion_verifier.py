# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
LLM-assisted verification for SAME_AS suggestions.

TASK-144 Phase 3: Verifies mid-confidence suggestions using LLM analysis
of stored entity attributes. No live API calls needed - entities already
have raw_attributes containing IPs, hostnames, specs, etc.

Flow:
1. HostnameMatcher creates suggestion with confidence 0.70-0.89
2. SuggestionVerifier calls confirm_same_as_with_llm() with both entities
3. If LLM confirms with high confidence → auto-approve
4. If LLM rejects → auto-reject
5. If uncertain → leave pending for manual review
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.config import get_config
from meho_app.core.otel import get_logger

from .correlation import LLMCorrelationResult, confirm_same_as_with_llm
from .models import TopologySameAsSuggestionModel
from .repository import TopologyRepository

logger = get_logger(__name__)


class SuggestionVerifier:
    """
    Verifies SAME_AS suggestions using LLM analysis of stored entity attributes.

    Uses the existing confirm_same_as_with_llm() function from correlation.py
    which compares entity descriptions and raw_attributes to determine if
    two entities represent the same physical/logical resource.

    Usage:
        verifier = SuggestionVerifier(session)

        # Verify a single suggestion
        result = await verifier.verify_suggestion(suggestion)

        # Or verify and auto-resolve based on LLM confidence
        resolved = await verifier.process_and_resolve(suggestion_id)
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = TopologyRepository(session)
        self._config = get_config()

    async def verify_suggestion(
        self,
        suggestion: TopologySameAsSuggestionModel,
    ) -> LLMCorrelationResult | None:
        """
        Verify a suggestion using LLM analysis.

        Loads both entities and calls confirm_same_as_with_llm() to analyze
        their attributes and determine if they represent the same resource.

        Args:
            suggestion: The suggestion to verify

        Returns:
            LLMCorrelationResult with is_same_resource, confidence, reasoning
            None if verification fails
        """
        # Load both entities with full attributes
        entity_a = await self.repository.get_entity_by_id(suggestion.entity_a_id)
        entity_b = await self.repository.get_entity_by_id(suggestion.entity_b_id)

        if not entity_a or not entity_b:
            logger.warning(
                f"Cannot verify suggestion {suggestion.id}: "
                f"entity_a={entity_a is not None}, entity_b={entity_b is not None}"
            )
            return None

        # Get connector names for better LLM context
        connector_a_name = entity_a.connector_name
        connector_b_name = entity_b.connector_name

        # Use existing LLM correlation function
        result = await confirm_same_as_with_llm(
            entity_a=entity_a,
            entity_b=entity_b,
            connector_a_name=connector_a_name,
            connector_b_name=connector_b_name,
        )

        if result:
            logger.info(
                f"LLM verification for {entity_a.name} ↔ {entity_b.name}: "
                f"is_same={result.is_same_resource}, confidence={result.confidence:.2f}"
            )

        return result

    async def process_and_resolve(
        self,
        suggestion_id: UUID,
        auto_approve_threshold: float | None = None,
    ) -> str:
        """
        Verify a suggestion and automatically resolve based on LLM confidence.

        Resolution logic:
        - If LLM says is_same_resource=True with confidence >= threshold → approve
        - If LLM says is_same_resource=False with confidence >= threshold → reject
        - If LLM is uncertain or verification fails → leave pending

        Args:
            suggestion_id: ID of the suggestion to verify
            auto_approve_threshold: LLM confidence required to auto-resolve
                                   (default from config: 0.80)

        Returns:
            New status: "approved", "rejected", or "pending"
        """
        threshold = auto_approve_threshold or self._config.suggestion_llm_approve_confidence

        # Load suggestion
        suggestion = await self.repository.get_suggestion_by_id(suggestion_id)
        if not suggestion:
            logger.warning(f"Suggestion {suggestion_id} not found")
            return "pending"

        if suggestion.status != "pending":
            logger.debug(f"Suggestion {suggestion_id} already resolved: {suggestion.status}")
            return suggestion.status

        # Already verified?
        if suggestion.llm_verification_attempted:
            logger.debug(f"Suggestion {suggestion_id} already verified")
            return suggestion.status

        # Run LLM verification
        result = await self.verify_suggestion(suggestion)

        # Store verification result
        await self.repository.update_suggestion_verification(
            suggestion_id=suggestion_id,
            llm_result=result.model_dump() if result else None,
        )

        if not result:
            logger.info(f"LLM verification failed for suggestion {suggestion_id}, leaving pending")
            return "pending"

        # Determine resolution based on LLM result
        if result.is_same_resource and result.confidence >= threshold:
            # LLM confidently says they're the same → approve
            await self.repository.approve_suggestion(
                suggestion_id=suggestion_id,
                user_id="llm_verification",
            )
            logger.info(
                f"Auto-approved suggestion {suggestion_id}: "
                f"LLM confidence={result.confidence:.2f}, reasoning={result.reasoning[:100]}..."
            )
            return "approved"

        elif not result.is_same_resource and result.confidence >= threshold:
            # LLM confidently says they're different → reject
            await self.repository.reject_suggestion(
                suggestion_id=suggestion_id,
                user_id="llm_verification",
            )
            logger.info(
                f"Auto-rejected suggestion {suggestion_id}: "
                f"LLM confidence={result.confidence:.2f}, reasoning={result.reasoning[:100]}..."
            )
            return "rejected"

        else:
            # LLM uncertain → leave for manual review
            logger.info(
                f"LLM uncertain about suggestion {suggestion_id} "
                f"(is_same={result.is_same_resource}, confidence={result.confidence:.2f}), "
                "leaving for manual review"
            )
            return "pending"


async def get_suggestion_verifier(session: AsyncSession) -> SuggestionVerifier:
    """Get a SuggestionVerifier instance for dependency injection."""
    return SuggestionVerifier(session)
