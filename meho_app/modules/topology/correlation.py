# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Correlation service for discovering SAME_AS relationships.

Finds potentially related entities across connectors using embedding similarity,
then uses LLM to confirm correlations (hybrid approach).

The flow:
1. Store entity → Generate embedding → Check for similar entities in RELATED connectors
2. Use LLM to analyze attributes and confirm if entities are the same resource
3. Store confirmed SAME_AS with LLM reasoning as evidence

This enables automatic cross-connector topology linking, e.g.:
- K8s Node ↔ GCP VM (same physical machine)
- K8s Node ↔ Proxmox VM
- K8s Node ↔ vSphere VM
"""

from uuid import UUID

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger

from .embedding import TopologyEmbeddingService, get_topology_embedding_service
from .models import TopologyEntityModel
from .repository import TopologyRepository
from .schemas import ConfirmedSameAs, PossiblyRelatedEntity

logger = get_logger(__name__)


# =============================================================================
# LLM Correlation Confirmation
# =============================================================================


class LLMCorrelationResult(BaseModel):
    """Structured output from LLM correlation analysis."""

    is_same_resource: bool = Field(
        ...,
        description="True if the two entities represent the same physical/logical resource",
    )
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence score (0-1) in the determination"
    )
    reasoning: str = Field(
        ..., description="Explanation of why the entities are or are not the same"
    )
    matching_identifiers: list[str] = Field(
        default_factory=list,
        description="List of matching identifiers found (e.g., 'IP: 10.0.0.5', 'hostname: node-01')",
    )


CORRELATION_PROMPT = """You are analyzing two infrastructure entities from different management systems to determine if they represent the SAME physical or logical resource.

## Entity A (from {connector_a})
Name: {name_a}
Description: {description_a}
Attributes: {attributes_a}

## Entity B (from {connector_b})
Name: {name_b}
Description: {description_b}
Attributes: {attributes_b}

## Your Task
Determine if these entities represent the SAME underlying resource. Look for:

1. **Matching Identifiers**: Same IP addresses, hostnames, provider IDs, resource names
2. **Provider References**: K8s nodes often have `providerId` like "gce://project/zone/vm-name"
3. **Logical Equivalence**: A K8s Node and its underlying VM are the same resource
4. **Name Patterns**: VM names often match K8s node names or contain them

## Common Patterns
- K8s Node `gke-cluster-pool-abc123` corresponds to GCP VM `gke-cluster-pool-abc123`
- K8s Node with `providerId: gce://project/zone/vm-name` matches GCP VM `vm-name`
- Proxmox VM hostname matches K8s node name
- IP addresses are strong indicators when they match

## Output
Provide your analysis as structured JSON. Be conservative - only confirm if you have strong evidence."""


async def confirm_same_as_with_llm(
    entity_a: TopologyEntityModel,
    entity_b: TopologyEntityModel,
    connector_a_name: str | None = None,
    connector_b_name: str | None = None,
) -> LLMCorrelationResult | None:
    """
    Use LLM to determine if two entities are the same physical resource.

    This is the second step in hybrid correlation:
    1. Embedding similarity finds candidates (pre-filter)
    2. LLM analyzes attributes to confirm (this function)

    Args:
        entity_a: First entity
        entity_b: Second entity
        connector_a_name: Display name for entity A's connector
        connector_b_name: Display name for entity B's connector

    Returns:
        LLMCorrelationResult with is_same_resource, confidence, and reasoning
        None if LLM call fails
    """
    try:
        # Create analysis agent with structured output
        from pydantic_ai import InstrumentationSettings

        from meho_app.core.config import get_config

        config = get_config()
        agent = Agent(
            config.classifier_model,
            output_type=LLMCorrelationResult,
            instructions="You analyze infrastructure entities to find cross-system correlations. Output valid JSON only.",
            instrument=InstrumentationSettings(),
        )

        # Format attributes for readability
        import json

        attrs_a = json.dumps(entity_a.raw_attributes or {}, indent=2, default=str)
        attrs_b = json.dumps(entity_b.raw_attributes or {}, indent=2, default=str)

        # Truncate if too long
        if len(attrs_a) > 3000:
            attrs_a = attrs_a[:3000] + "\n... (truncated)"
        if len(attrs_b) > 3000:
            attrs_b = attrs_b[:3000] + "\n... (truncated)"

        prompt = CORRELATION_PROMPT.format(
            connector_a=connector_a_name or str(entity_a.connector_id) or "Unknown",
            name_a=entity_a.name,
            description_a=entity_a.description,
            attributes_a=attrs_a,
            connector_b=connector_b_name or str(entity_b.connector_id) or "Unknown",
            name_b=entity_b.name,
            description_b=entity_b.description,
            attributes_b=attrs_b,
        )

        result = await agent.run(prompt)

        return result.output

    except Exception as e:
        logger.warning(f"LLM correlation failed for {entity_a.name} ↔ {entity_b.name}: {e}")
        return None


class CorrelationService:
    """
    Service for discovering cross-connector entity correlations.

    Uses embedding similarity to find entities that might represent
    the same real-world thing across different connectors.

    Example:
        - K8s Node "node-01" with IP 192.168.1.10
        - Proxmox VM "k8s-worker-01" with IP 192.168.1.10

        These have similar embeddings because their descriptions mention
        the same IP address and similar roles (worker node, VM).

        The agent then verifies by calling both APIs and confirming the IPs match.
    """

    # Minimum similarity score to consider entities as potentially related
    DEFAULT_SIMILARITY_THRESHOLD = 0.7

    # Maximum number of possibly related entities to return
    DEFAULT_MAX_RELATED = 5

    def __init__(
        self,
        session: AsyncSession,
        embedding_service: TopologyEmbeddingService | None = None,
    ) -> None:
        self.session = session
        self.repository = TopologyRepository(session)
        self.embedding_service = embedding_service or get_topology_embedding_service()

    async def find_possibly_related(
        self,
        entity: TopologyEntityModel,
        min_similarity: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_results: int = DEFAULT_MAX_RELATED,
    ) -> list[PossiblyRelatedEntity]:
        """
        Find entities that might be related to the given entity.

        Searches for similar entities in OTHER connectors (cross-connector only).

        Args:
            entity: The entity to find relations for
            min_similarity: Minimum similarity score (0-1)
            max_results: Maximum number of results

        Returns:
            List of possibly related entities with similarity scores

        Example:
            # After storing a K8s Node, find possibly related VMs
            related = await correlation.find_possibly_related(node_entity)
            # Returns: [PossiblyRelatedEntity(entity="k8s-worker-01", similarity=0.85)]
        """
        if not entity.embedding:
            logger.debug(f"Entity {entity.name} has no embedding, cannot find related")
            return []

        # Get the embedding vector
        embedding_vector = list(entity.embedding.embedding)

        # Find similar entities in OTHER connectors
        similar_entities = await self.repository.find_similar_entities(
            embedding=embedding_vector,
            tenant_id=entity.tenant_id,
            exclude_entity_id=entity.id,
            exclude_connector_id=entity.connector_id,  # Only cross-connector
            limit=max_results,
            min_similarity=min_similarity,
        )

        # Convert to schema
        result = []
        for similar_entity, similarity in similar_entities:
            result.append(
                PossiblyRelatedEntity(
                    entity=similar_entity.name,
                    connector_id=similar_entity.connector_id,
                    similarity=similarity,
                )
            )

        if result:
            logger.info(
                f"Found {len(result)} possibly related entities for {entity.name}: "
                f"{[r.entity for r in result]}"
            )

        return result

    async def generate_and_store_embedding(
        self,
        entity: TopologyEntityModel,
    ) -> list[float]:
        """
        Generate embedding for an entity and store it.

        Called after creating a new entity.

        Args:
            entity: The entity to generate embedding for

        Returns:
            The generated embedding vector
        """
        # Generate embedding from description
        embedding = await self.embedding_service.generate_embedding(entity.description)

        # Store it
        await self.repository.store_embedding(entity.id, embedding)
        await self.session.flush()

        logger.debug(f"Stored embedding for entity {entity.name}")

        return embedding

    async def check_for_correlations(
        self,
        entity: TopologyEntityModel,
        min_similarity: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> list[PossiblyRelatedEntity]:
        """
        Generate embedding for entity and find possibly related entities.

        Convenience method that combines:
        1. Generate and store embedding
        2. Find similar entities

        Args:
            entity: The newly created entity
            min_similarity: Minimum similarity for "possibly related"

        Returns:
            List of possibly related entities to investigate
        """
        # Generate and store embedding
        await self.generate_and_store_embedding(entity)

        # Reload entity with embedding relationship
        refreshed_entity = await self.repository.get_entity_by_id(entity.id)
        if not refreshed_entity:
            return []

        # Find similar entities in OTHER connectors
        return await self.find_possibly_related(
            entity=refreshed_entity,
            min_similarity=min_similarity,
        )

    async def find_similar_in_connectors(
        self,
        entity: TopologyEntityModel,
        target_connector_ids: list[UUID],
        min_similarity: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_results: int = DEFAULT_MAX_RELATED,
    ) -> list[tuple[TopologyEntityModel, float]]:
        """
        Find similar entities in specific connectors.

        Used for targeted cross-connector correlation when we know
        which connectors are related (via related_connector_ids).

        Args:
            entity: The entity to find correlations for
            target_connector_ids: Connectors to search in
            min_similarity: Minimum similarity threshold
            max_results: Maximum results per connector

        Returns:
            List of (entity, similarity_score) tuples
        """
        if not entity.embedding:
            logger.debug(f"Entity {entity.name} has no embedding, cannot find similar")
            return []

        if not target_connector_ids:
            return []

        embedding_vector = list(entity.embedding.embedding)
        all_similar = []

        for connector_id in target_connector_ids:
            similar = await self.repository.find_similar_entities(
                embedding=embedding_vector,
                tenant_id=entity.tenant_id,
                exclude_entity_id=entity.id,
                filter_connector_id=connector_id,  # Only this connector
                limit=max_results,
                min_similarity=min_similarity,
            )
            all_similar.extend(similar)

        # Sort by similarity and limit
        all_similar.sort(key=lambda x: x[1], reverse=True)
        return all_similar[:max_results]

    async def find_and_confirm_correlations(  # NOSONAR (cognitive complexity)
        self,
        entity: TopologyEntityModel,
        related_connector_ids: list[UUID],
        connector_names: dict[UUID, str] | None = None,
        min_similarity: float = DEFAULT_SIMILARITY_THRESHOLD,
        min_llm_confidence: float = 0.7,
    ) -> list[ConfirmedSameAs]:
        """
        Hybrid correlation: embedding pre-filter + LLM confirmation.

        This is the main method for automatic cross-connector correlation.

        Flow:
        1. Use embedding similarity to find candidates in related connectors
        2. For each candidate, use LLM to analyze attributes
        3. Return confirmed correlations with LLM reasoning

        Args:
            entity: The entity to find correlations for
            related_connector_ids: Connectors to search in (from connector.related_connector_ids)
            connector_names: Optional mapping of connector_id → name for better LLM context
            min_similarity: Minimum embedding similarity for candidates
            min_llm_confidence: Minimum LLM confidence to confirm correlation

        Returns:
            List of ConfirmedSameAs with LLM reasoning
        """
        if not related_connector_ids:
            return []

        # Ensure entity has embedding
        if not entity.embedding:
            await self.generate_and_store_embedding(entity)
            refreshed = await self.repository.get_entity_by_id(entity.id)
            if not refreshed:
                return []
            entity = refreshed

        # Step 1: Find similar entities in related connectors (pre-filter)
        candidates = await self.find_similar_in_connectors(
            entity=entity,
            target_connector_ids=related_connector_ids,
            min_similarity=min_similarity,
        )

        if not candidates:
            logger.debug(f"No correlation candidates for {entity.name} in related connectors")
            return []

        logger.info(
            f"Found {len(candidates)} correlation candidates for {entity.name}: "
            f"{[c[0].name for c in candidates]}"
        )

        # Step 2: Use LLM to confirm each candidate
        confirmed = []
        connector_names = connector_names or {}

        for candidate_entity, similarity in candidates:
            # Get connector names for better LLM context
            entity_connector_name = (
                connector_names.get(entity.connector_id, entity.connector_name)
                if entity.connector_id
                else None
            )

            candidate_connector_name = (
                connector_names.get(candidate_entity.connector_id, candidate_entity.connector_name)
                if candidate_entity.connector_id
                else None
            )

            # LLM analysis
            llm_result = await confirm_same_as_with_llm(
                entity_a=entity,
                entity_b=candidate_entity,
                connector_a_name=entity_connector_name,
                connector_b_name=candidate_connector_name,
            )

            if not llm_result:
                continue

            if llm_result.is_same_resource and llm_result.confidence >= min_llm_confidence:
                # Confirmed!
                confirmed.append(
                    ConfirmedSameAs(
                        entity_a_name=entity.name,
                        entity_b_name=candidate_entity.name,
                        entity_a_connector_id=entity.connector_id,
                        entity_b_connector_id=candidate_entity.connector_id,
                        similarity_score=similarity,
                        llm_confidence=llm_result.confidence,
                        reasoning=llm_result.reasoning,
                        verified_via=[
                            "embedding_similarity",
                            "llm_analysis",
                            *llm_result.matching_identifiers,
                        ],
                    )
                )

                logger.info(
                    f"Confirmed SAME_AS: {entity.name} ↔ {candidate_entity.name} "
                    f"(similarity={similarity:.2f}, llm_confidence={llm_result.confidence:.2f})"
                )
            else:
                logger.debug(
                    f"Rejected correlation: {entity.name} ↔ {candidate_entity.name} "
                    f"(is_same={llm_result.is_same_resource}, confidence={llm_result.confidence:.2f})"
                )

        return confirmed


def get_correlation_service(
    session: AsyncSession,
    embedding_service: TopologyEmbeddingService | None = None,
) -> CorrelationService:
    """Get a CorrelationService instance for dependency injection."""
    return CorrelationService(session, embedding_service)
