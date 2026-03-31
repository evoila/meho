# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Proactive SAME_AS discovery using embedding similarity clustering.

TASK-160 Phase 2: Periodically scans entity embeddings to find similar entities
across different connectors, then creates suggestions for user review.

The flow:
1. Query pgvector for high-similarity cross-connector pairs
2. Filter by SameAsEligibility rules (Node ↔ VM ✅, Pod ↔ VM ❌)
3. Check if suggestion already exists (avoid duplicates)
4. Create pending suggestions for new discoveries
5. Optionally run LLM verification for mid-confidence matches

Key design decisions:
- Never hard delete suggestions (rejected ones stay to prevent re-discovery)
- Eligibility is symmetric: if A can match B, check both schemas
- Use existing repository methods for CRUD operations
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger

from .models import TopologyEntityModel
from .repository import TopologyRepository
from .schema import get_topology_schema
from .schemas import SameAsSuggestionCreate

logger = get_logger(__name__)


@dataclass
class DiscoveryResult:
    """Result of a SAME_AS discovery run."""

    suggestions_created: int
    suggestions_skipped_existing: int
    suggestions_skipped_ineligible: int
    total_pairs_analyzed: int

    @property
    def message(self) -> str:
        """Human-readable summary."""
        return (
            f"Created {self.suggestions_created} new suggestions. "
            f"Skipped {self.suggestions_skipped_existing} existing, "
            f"{self.suggestions_skipped_ineligible} ineligible "
            f"(from {self.total_pairs_analyzed} pairs analyzed)."
        )


class ClusteringService:
    """
    Discovers SAME_AS candidates by finding similar embeddings across connectors.

    Uses pgvector cosine similarity to find entity pairs that might represent
    the same physical/logical resource across different connectors (e.g.,
    K8s Node and VMware VM).

    Flow:
    1. Query pgvector for high-similarity cross-connector pairs
    2. Filter by SameAsEligibility rules (from Phase 1 schemas)
    3. Check for existing suggestions (never recreate)
    4. Create new pending suggestions

    Usage:
        service = ClusteringService(session)
        result = await service.discover_same_as_candidates(
            tenant_id="tenant-123",
            min_similarity=0.70,
            limit=50,
        )
        print(f"Created {result.suggestions_created} suggestions")
    """

    DEFAULT_SIMILARITY_THRESHOLD = 0.70
    DEFAULT_BATCH_SIZE = 100

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repository = TopologyRepository(session)

    async def discover_same_as_candidates(
        self,
        tenant_id: str,
        min_similarity: float = DEFAULT_SIMILARITY_THRESHOLD,
        limit: int = DEFAULT_BATCH_SIZE,
    ) -> DiscoveryResult:
        """
        Find cross-connector entity pairs with high embedding similarity.

        Only creates NEW suggestions (not already in suggestion table).
        Filters by SameAsEligibility rules to prevent nonsensical matches.

        Args:
            tenant_id: Tenant ID to scope the search
            min_similarity: Minimum embedding similarity (0-1, default 0.70)
            limit: Maximum suggestions to create (default 100)

        Returns:
            DiscoveryResult with counts of created, skipped, and analyzed pairs

        Example:
            result = await service.discover_same_as_candidates(
                tenant_id="tenant-123",
                min_similarity=0.75,
                limit=25,
            )
            # result.suggestions_created = 12
            # result.suggestions_skipped_existing = 5
        """
        # Step 1: Query pgvector for similar pairs across connectors
        candidates = await self.repository.find_cross_connector_similar_pairs(
            tenant_id=tenant_id,
            min_similarity=min_similarity,
            limit=limit * 3,  # Over-fetch to account for filtering
        )

        logger.info(f"Found {len(candidates)} raw similarity candidates for tenant {tenant_id}")

        created = 0
        skipped_existing = 0
        skipped_ineligible = 0

        for entity_a_id, entity_b_id, similarity in candidates:
            # Load entities for eligibility check
            entity_a = await self.repository.get_entity_by_id(entity_a_id)
            entity_b = await self.repository.get_entity_by_id(entity_b_id)

            if not entity_a or not entity_b:
                logger.debug("Skipping pair: entity not found")
                continue

            # Step 2: Check SameAsEligibility
            if not self._is_eligible_pair(entity_a, entity_b):
                logger.debug(
                    f"Skipping ineligible pair: {entity_a.entity_type} ↔ {entity_b.entity_type}"
                )
                skipped_ineligible += 1
                continue

            # Step 3: Check if suggestion already exists (ANY status)
            existing = await self.repository.get_existing_suggestion(
                entity_a_id=entity_a.id,
                entity_b_id=entity_b.id,
            )

            if existing:
                logger.debug(
                    f"Skipping existing suggestion: {entity_a.name} ↔ {entity_b.name} "
                    f"(status={existing.status})"
                )
                skipped_existing += 1
                continue

            # Also check if SAME_AS already exists
            existing_same_as = await self.repository.check_existing_same_as(
                entity_a_id=entity_a.id,
                entity_b_id=entity_b.id,
            )

            if existing_same_as:
                logger.debug(
                    f"Skipping - SAME_AS already exists: {entity_a.name} ↔ {entity_b.name}"
                )
                skipped_existing += 1
                continue

            # Step 4: Create new suggestion
            suggestion_input = SameAsSuggestionCreate(
                entity_a_id=entity_a.id,
                entity_b_id=entity_b.id,
                confidence=similarity,
                match_type="embedding_similarity",
                match_details=self._build_match_details(entity_a, entity_b, similarity),
            )

            await self.repository.create_suggestion(
                suggestion=suggestion_input,
                tenant_id=tenant_id,
            )

            logger.info(
                f"Created SAME_AS suggestion: {entity_a.name} ({entity_a.entity_type}) ↔ "
                f"{entity_b.name} ({entity_b.entity_type}) - confidence {similarity:.2%}"
            )

            created += 1

            if created >= limit:
                break

        return DiscoveryResult(
            suggestions_created=created,
            suggestions_skipped_existing=skipped_existing,
            suggestions_skipped_ineligible=skipped_ineligible,
            total_pairs_analyzed=len(candidates),
        )

    def _is_eligible_pair(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
    ) -> bool:
        """
        Check if two entities can have SAME_AS based on schema rules.

        Eligibility is symmetric: if A's schema allows matching B's type,
        OR if B's schema allows matching A's type, the pair is eligible.

        Args:
            entity_a: First entity
            entity_b: Second entity

        Returns:
            True if SAME_AS is allowed between these entity types
        """
        # Get schemas for both connectors
        schema_a = get_topology_schema(entity_a.connector_type)
        schema_b = get_topology_schema(entity_b.connector_type)

        if not schema_a or not schema_b:
            # Unknown connector types - be conservative, reject
            logger.debug(
                f"Unknown schema for {entity_a.connector_type} or {entity_b.connector_type}"
            )
            return False

        # Get entity type definitions
        type_def_a = schema_a.get_entity_definition(entity_a.entity_type)
        type_def_b = schema_b.get_entity_definition(entity_b.entity_type)

        if not type_def_a or not type_def_b:
            # Unknown entity types - be conservative, reject
            logger.debug(f"Unknown entity type: {entity_a.entity_type} or {entity_b.entity_type}")
            return False

        # Check if A can match B's type
        if type_def_a.same_as and type_def_a.same_as.can_correlate_with(entity_b.entity_type):
            return True

        # Check if B can match A's type (symmetric)
        return bool(
            type_def_b.same_as and type_def_b.same_as.can_correlate_with(entity_a.entity_type)
        )

    def _build_match_details(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
        similarity: float,
    ) -> str:
        """Build human-readable match details for the suggestion."""
        parts = [
            f"Embedding similarity: {similarity:.1%}",
            f"Types: {entity_a.connector_type}.{entity_a.entity_type} ↔ "
            f"{entity_b.connector_type}.{entity_b.entity_type}",
        ]

        # Add connector names if available
        if entity_a.connector_name and entity_b.connector_name:
            parts.append(f"Connectors: {entity_a.connector_name} ↔ {entity_b.connector_name}")

        return "; ".join(parts)


async def run_same_as_discovery(
    session: AsyncSession,
    tenant_id: str,
    min_similarity: float = 0.70,
    limit: int = 50,
) -> DiscoveryResult:
    """
    Convenience function to run SAME_AS discovery.

    Can be called from:
    - API endpoint (on-demand)
    - Background job (scheduled)
    - CLI command

    Args:
        session: Database session
        tenant_id: Tenant to run discovery for
        min_similarity: Minimum embedding similarity threshold
        limit: Maximum suggestions to create

    Returns:
        DiscoveryResult with counts and summary message
    """
    service = ClusteringService(session)
    return await service.discover_same_as_candidates(
        tenant_id=tenant_id,
        min_similarity=min_similarity,
        limit=limit,
    )


def get_clustering_service(session: AsyncSession) -> ClusteringService:
    """Get a ClusteringService instance for dependency injection."""
    return ClusteringService(session)
