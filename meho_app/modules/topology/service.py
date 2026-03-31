# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology service - main interface for topology operations.

This service is the public interface for the topology module.
It coordinates entity storage, embedding generation, and graph traversal.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.otel import get_logger

from .correlation import CorrelationService
from .embedding import TopologyEmbeddingService, get_topology_embedding_service
from .models import TopologyEntityModel
from .repository import TopologyRepository
from .schema import get_topology_schema
from .schemas import (
    CorrelatedEntity,
    InvalidateTopologyInput,
    InvalidateTopologyResult,
    LookupTopologyInput,
    LookupTopologyResult,
    PossiblyRelatedEntity,
    SameAsSuggestionCreate,
    StoreDiscoveryInput,
    StoreDiscoveryResult,
    TopologyEntity,
    TopologySameAsCreate,
)

logger = get_logger(__name__)


class TopologyService:
    """
    Service for managing learned system topology.

    The agent learns topology through investigation:
    1. Discovers entities (Pods, VMs, Ingresses, etc.)
    2. Stores them with rich descriptions for embedding
    3. Finds relationships between entities
    4. Correlates entities across connectors via SAME_AS

    Usage:
        service = TopologyService(session)

        # Store discoveries
        result = await service.store_discovery(input, tenant_id)

        # Lookup topology
        result = await service.lookup(input, tenant_id)

        # Invalidate stale data
        result = await service.invalidate(input, tenant_id)
    """

    def __init__(
        self,
        session: AsyncSession,
        embedding_service: TopologyEmbeddingService | None = None,
    ):
        self.session = session
        self.repository = TopologyRepository(session)
        self.embedding_service = embedding_service or get_topology_embedding_service()
        self.correlation_service = CorrelationService(session, self.embedding_service)

    # =========================================================================
    # Store Discovery
    # =========================================================================

    async def store_discovery(
        self,
        input: StoreDiscoveryInput,
        tenant_id: str,
    ) -> StoreDiscoveryResult:
        """
        Store discovered topology (entities, relationships, SAME_AS).

        The agent calls this after investigating systems to remember
        what it learned for future requests.

        Validates entities and relationships against the connector's topology schema:
        - Invalid entity types are rejected with clear error messages
        - Invalid relationships are rejected with clear error messages
        - Unknown connector types skip validation (allow anything)
        - Entities are deduplicated by canonical ID
        """
        entities_created = 0
        relationships_created = 0
        same_as_created = 0
        validation_errors: list[str] = []
        # Map entity name to (entity_id, entity_type) for relationship validation
        entity_id_map: dict[str, tuple[UUID, str]] = {}

        # Get schema for connector type (may be None for unknown types like "rest")
        schema = get_topology_schema(input.connector_type)

        try:
            # 1. Store entities with schema validation
            for entity_input in input.entities:
                # Validate entity type against schema
                if schema and not schema.is_valid_entity_type(entity_input.entity_type):
                    error_msg = (
                        f"Invalid entity type '{entity_input.entity_type}' "
                        f"for connector type '{input.connector_type}'. "
                        f"Valid types: {', '.join(sorted(schema.get_all_entity_types()))}"
                    )
                    validation_errors.append(error_msg)
                    logger.warning(error_msg)
                    continue

                # Build canonical ID using schema or fallback
                if schema:
                    canonical_id = schema.build_canonical_id(
                        entity_input.entity_type,
                        entity_input.scope or {},
                        entity_input.name,
                    )
                elif entity_input.canonical_id:
                    canonical_id = entity_input.canonical_id
                elif entity_input.scope:
                    # Build canonical_id from scope values + name
                    scope_parts = [str(v) for v in entity_input.scope.values()]
                    canonical_id = "/".join([*scope_parts, entity_input.name])
                else:
                    canonical_id = entity_input.name

                # Ensure connector_type is set on entity
                if not entity_input.connector_type:
                    entity_input.connector_type = input.connector_type

                # Ensure connector_id is set on entity (from input or entity)
                if not entity_input.connector_id and input.connector_id:
                    entity_input.connector_id = input.connector_id

                # Upsert entity by canonical identity
                db_entity, is_new = await self.repository.upsert_entity(
                    entity=entity_input,
                    tenant_id=tenant_id,
                    canonical_id=canonical_id,
                )

                entity_id_map[entity_input.name] = (db_entity.id, entity_input.entity_type)

                if is_new:
                    entities_created += 1

                    # Generate and store embedding for new entities
                    try:
                        embedding = await self.embedding_service.generate_embedding(
                            entity_input.description
                        )
                        await self.repository.store_embedding(db_entity.id, embedding)
                    except Exception as e:
                        # Log but don't fail - embedding is optional
                        logger.warning(f"Failed to generate embedding for {entity_input.name}: {e}")

            # 2. Store relationships with schema validation
            for rel_input in input.relationships:
                # Get entity IDs and types from the map
                from_info = entity_id_map.get(rel_input.from_entity_name)
                to_info = entity_id_map.get(rel_input.to_entity_name)

                # If entities weren't in current batch, try to resolve them.
                # Pass connector_id for within-connector relationship precision.
                if not from_info:
                    from_entity = await self._resolve_entity(
                        rel_input.from_entity_name,
                        tenant_id,
                        connector_id=input.connector_id,
                    )
                    if from_entity:
                        from_info = (from_entity.id, from_entity.entity_type)

                if not to_info:
                    to_entity = await self._resolve_entity(
                        rel_input.to_entity_name,
                        tenant_id,
                        connector_id=input.connector_id,
                    )
                    if to_entity:
                        to_info = (to_entity.id, to_entity.entity_type)

                # Skip if either entity not found
                if not from_info or not to_info:
                    error_msg = (
                        f"Cannot create relationship: entity not found "
                        f"(from='{rel_input.from_entity_name}', to='{rel_input.to_entity_name}')"
                    )
                    validation_errors.append(error_msg)
                    logger.warning(error_msg)
                    continue

                from_id, from_type = from_info
                to_id, to_type = to_info

                # Validate relationship against schema
                if schema and not schema.is_valid_relationship(
                    from_type, rel_input.relationship_type, to_type
                ):
                    error_msg = (
                        f"Invalid relationship: {from_type} --{rel_input.relationship_type}--> {to_type} "
                        f"is not allowed for connector type '{input.connector_type}'"
                    )
                    validation_errors.append(error_msg)
                    logger.warning(error_msg)
                    continue

                # Check if relationship already exists
                existing_rel = await self.repository.get_relationship(
                    from_entity_id=from_id,
                    to_entity_id=to_id,
                    relationship_type=rel_input.relationship_type,
                )

                if not existing_rel:
                    await self.repository.create_relationship(
                        from_entity_id=from_id,
                        to_entity_id=to_id,
                        relationship_type=rel_input.relationship_type,
                    )
                    relationships_created += 1
                    logger.info(
                        f"Created relationship: {rel_input.from_entity_name} "
                        f"--{rel_input.relationship_type}--> {rel_input.to_entity_name}"
                    )

            # 3. Store SAME_AS (requires verified_via)
            for same_as_input in input.same_as:
                if not same_as_input.verified_via:
                    logger.warning(
                        f"Skipping SAME_AS without verification: "
                        f"{same_as_input.entity_a_name} <-> {same_as_input.entity_b_name}"
                    )
                    continue

                # Get entity IDs from the map first, then resolve
                entity_a_info = entity_id_map.get(same_as_input.entity_a_name)
                entity_b_info = entity_id_map.get(same_as_input.entity_b_name)

                if not entity_a_info:
                    entity_a = await self._resolve_entity(
                        same_as_input.entity_a_name,
                        tenant_id,
                        connector_id=same_as_input.entity_a_connector_id,
                        entity_type=same_as_input.entity_a_type,
                    )
                    if entity_a:
                        entity_a_info = (entity_a.id, entity_a.entity_type)

                if not entity_b_info:
                    entity_b = await self._resolve_entity(
                        same_as_input.entity_b_name,
                        tenant_id,
                        connector_id=same_as_input.entity_b_connector_id,
                        entity_type=same_as_input.entity_b_type,
                    )
                    if entity_b:
                        entity_b_info = (entity_b.id, entity_b.entity_type)

                if entity_a_info and entity_b_info:
                    await self.repository.create_same_as(
                        entity_a_id=entity_a_info[0],
                        entity_b_id=entity_b_info[0],
                        similarity_score=same_as_input.similarity_score,
                        verified_via=same_as_input.verified_via,
                        tenant_id=tenant_id,
                    )
                    same_as_created += 1
                    logger.info(
                        f"Created SAME_AS: {same_as_input.entity_a_name} <-> "
                        f"{same_as_input.entity_b_name} (verified: {same_as_input.verified_via})"
                    )

            await self.session.commit()

            # Build message
            parts = []
            if entities_created:
                parts.append(f"{entities_created} entities")
            if relationships_created:
                parts.append(f"{relationships_created} relationships")
            if same_as_created:
                parts.append(f"{same_as_created} SAME_AS links")

            message = f"Stored {', '.join(parts)}." if parts else "Nothing new to store."

            if validation_errors:
                message += f" {len(validation_errors)} validation error(s)."

            return StoreDiscoveryResult(
                stored=True,
                entities_created=entities_created,
                relationships_created=relationships_created,
                same_as_created=same_as_created,
                validation_errors=validation_errors,
                message=message,
            )

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to store discovery: {e}")
            return StoreDiscoveryResult(
                stored=False,
                validation_errors=validation_errors,
                message=f"Failed to store: {e!s}",
            )

    async def _resolve_entity(
        self,
        name: str,
        tenant_id: str,
        connector_id: UUID | None = None,
        entity_type: str | None = None,
    ) -> TopologyEntityModel | None:
        """
        Resolve an entity name to a model using identity quad lookup.

        For precise resolution, provide connector_id and entity_type to avoid
        false matches when multiple entities share the same name (e.g., "nginx"
        exists as both a Pod and a Service in different connectors).

        Identity quad: (name, tenant_id, connector_id, entity_type)
        """
        return await self.repository.get_entity_by_name(
            name, tenant_id, connector_id=connector_id, entity_type=entity_type
        )

    async def store_same_as(
        self,
        input: TopologySameAsCreate,
        tenant_id: str,
    ) -> bool:
        """
        Store a single SAME_AS relationship.

        Used by automatic cross-connector correlation to store
        LLM-confirmed relationships.

        SECURITY: Validates that both entities belong to the same tenant before
        creating the SAME_AS relationship. Raises ValueError if tenants don't match.

        Args:
            input: SAME_AS relationship to store
            tenant_id: Tenant ID

        Returns:
            True if stored successfully, False otherwise
        """
        try:
            if not input.verified_via:
                logger.warning(
                    f"Skipping SAME_AS without verification: "
                    f"{input.entity_a_name} <-> {input.entity_b_name}"
                )
                return False

            entity_a = await self._resolve_entity(
                input.entity_a_name,
                tenant_id,
                connector_id=input.entity_a_connector_id,
                entity_type=input.entity_a_type,
            )
            entity_b = await self._resolve_entity(
                input.entity_b_name,
                tenant_id,
                connector_id=input.entity_b_connector_id,
                entity_type=input.entity_b_type,
            )

            if not entity_a:
                logger.warning(f"Entity not found for SAME_AS: {input.entity_a_name}")
                return False
            if not entity_b:
                logger.warning(f"Entity not found for SAME_AS: {input.entity_b_name}")
                return False

            # TENANT SAFETY: Validate both entities belong to the same tenant
            if entity_a.tenant_id != tenant_id:
                raise ValueError(
                    f"SAME_AS cannot cross tenant boundaries: entity_a '{input.entity_a_name}' "
                    f"belongs to tenant '{entity_a.tenant_id}', expected '{tenant_id}'"
                )
            if entity_b.tenant_id != tenant_id:
                raise ValueError(
                    f"SAME_AS cannot cross tenant boundaries: entity_b '{input.entity_b_name}' "
                    f"belongs to tenant '{entity_b.tenant_id}', expected '{tenant_id}'"
                )

            await self.repository.create_same_as(
                entity_a_id=entity_a.id,
                entity_b_id=entity_b.id,
                similarity_score=input.similarity_score,
                verified_via=input.verified_via,
                tenant_id=tenant_id,
            )

            await self.session.commit()

            logger.info(
                f"Created SAME_AS: {input.entity_a_name} <-> "
                f"{input.entity_b_name} (verified: {input.verified_via})"
            )
            return True

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to store SAME_AS: {e}")
            return False

    # =========================================================================
    # Deterministic Entity Resolution (Phase 15)
    # =========================================================================

    async def resolve_entity_pair(
        self,
        entity_a: TopologyEntityModel,
        entity_b: TopologyEntityModel,
        tenant_id: str,
    ) -> bool:
        """
        Run deterministic resolution on a pair of entities.

        Uses the DeterministicResolver to check if two entities represent
        the same physical resource. Creates a confirmed SAME_AS for
        high-confidence matches, or a suggestion for ambiguous matches.

        Args:
            entity_a: First entity
            entity_b: Second entity
            tenant_id: Tenant ID for scoping

        Returns:
            True if a match was found (SAME_AS or suggestion created), False otherwise
        """
        from meho_app.modules.topology.resolution import get_default_resolver

        resolver = get_default_resolver()
        evidence = resolver.resolve_pair(entity_a, entity_b)

        if not evidence:
            return False

        if evidence.auto_confirm:
            # High-confidence match: create confirmed SAME_AS
            existing = await self.repository.check_existing_same_as(entity_a.id, entity_b.id)
            if existing:
                return True  # Already exists, skip

            verified_via = [
                "deterministic_resolution",
                f"match_type:{evidence.match_type}",
                f"matched_values:{json.dumps(evidence.matched_values)}",
                f"confidence:{evidence.confidence}",
            ]

            await self.repository.create_same_as(
                entity_a_id=entity_a.id,
                entity_b_id=entity_b.id,
                similarity_score=evidence.confidence,
                verified_via=verified_via,
                tenant_id=tenant_id,
            )

            logger.info(
                f"Deterministic match: {entity_a.name} ({entity_a.entity_type}) "
                f"<-> {entity_b.name} ({entity_b.entity_type}) via {evidence.match_type}"
            )
            return True
        else:
            # Ambiguous match (e.g., hostname partial): create suggestion
            await self.repository.create_suggestion(
                SameAsSuggestionCreate(
                    entity_a_id=entity_a.id,
                    entity_b_id=entity_b.id,
                    confidence=evidence.confidence,
                    match_type=evidence.match_type,
                    match_details=json.dumps(evidence.matched_values),
                ),
                tenant_id=tenant_id,
            )
            return True

    async def _remove_stale_same_as(
        self,
        entity: TopologyEntityModel,
        tenant_id: str,
    ) -> int:
        """
        Re-validate existing deterministic SAME_AS relationships for an entity.

        For each existing deterministic SAME_AS, re-run the resolver. If the
        resolver no longer produces a match (e.g., IP changed), delete the
        stale relationship.

        Returns:
            Number of stale relationships removed.
        """
        from meho_app.modules.topology.resolution import get_default_resolver

        resolver = get_default_resolver()
        existing = await self.repository.get_deterministic_same_as_for_entity(entity.id)

        removed = 0
        for same_as in existing:
            # Determine the other entity in the pair
            other_id = (
                same_as.entity_b_id if same_as.entity_a_id == entity.id else same_as.entity_a_id
            )
            other_entity = await self.repository.get_entity_by_id(other_id)

            if not other_entity:
                # Other entity was deleted -- remove the stale SAME_AS
                await self.repository.delete_same_as(entity.id, other_id)
                removed += 1
                continue

            # Re-run resolution -- does the match still hold?
            evidence = resolver.resolve_pair(entity, other_entity)

            if evidence is None:
                # Match no longer holds -- delete the stale relationship
                await self.repository.delete_same_as(entity.id, other_id)
                removed += 1
                logger.info(
                    f"Removed stale SAME_AS: {entity.name} <-> {other_entity.name} "
                    f"(evidence no longer holds)"
                )

        return removed

    async def batch_resolve(
        self,
        connector_id: UUID,
        related_connector_ids: list[str],
        tenant_id: str,
    ) -> dict:
        """
        Run batch deterministic resolution across connector entity sets.

        Loads all entities for the connector and each related connector,
        then runs pairwise resolution. Creates SAME_AS relationships for
        confirmed matches and suggestions for ambiguous matches.

        Args:
            connector_id: The primary connector ID
            related_connector_ids: List of related connector ID strings
            tenant_id: Tenant ID for scoping

        Returns:
            Stats dict with matches_found, same_as_created, suggestions_created
        """
        from meho_app.modules.topology.resolution import get_default_resolver

        resolver = get_default_resolver()

        entities_a = await self.repository.get_entities_by_connector(
            connector_id, tenant_id, limit=500
        )

        matches_found = 0
        same_as_created = 0
        suggestions_created = 0

        for related_id_str in related_connector_ids:
            try:
                related_connector_id = UUID(related_id_str)
            except (ValueError, AttributeError):
                logger.warning(f"Invalid related connector ID: {related_id_str}")
                continue

            entities_b = await self.repository.get_entities_by_connector(
                related_connector_id, tenant_id, limit=500
            )

            batch_results = resolver.resolve_batch(entities_a, entities_b)

            for entity_a, entity_b, evidence in batch_results:
                matches_found += 1
                result = await self.resolve_entity_pair(entity_a, entity_b, tenant_id)
                if result:
                    if evidence.auto_confirm:
                        same_as_created += 1
                    else:
                        suggestions_created += 1

        stats = {
            "matches_found": matches_found,
            "same_as_created": same_as_created,
            "suggestions_created": suggestions_created,
        }

        logger.info(
            f"Batch resolution for connector {connector_id}: "
            f"{matches_found} matches found, {same_as_created} SAME_AS created, "
            f"{suggestions_created} suggestions"
        )

        return stats

    # =========================================================================
    # Lookup
    # =========================================================================

    async def lookup(
        self,
        input: LookupTopologyInput,
        tenant_id: str,
    ) -> LookupTopologyResult:
        """
        Lookup topology by entity name.

        Uses a 2-stage search strategy:
        1. Exact name match (instant, no API call)
        2. Semantic search via embeddings (flexible, handles variations)

        Returns the full topology chain from the entity,
        traversing relationships and SAME_AS links.
        """
        # Stage 1: Exact name match (instant, no API call)
        entity = await self.repository.get_entity_by_name(
            name=input.query,
            tenant_id=tenant_id,
        )

        if not entity:
            # Stage 2: Semantic search via embeddings
            # This handles partial names, typos, namespace prefixes, and natural language queries
            # e.g., "bgodds-bgoddsservice" matches "bgoddsservice" via embedding similarity
            try:
                semantic_matches = await self._semantic_search(
                    query=input.query,
                    tenant_id=tenant_id,
                    limit=5,
                    min_similarity=0.6,
                )
                if semantic_matches:
                    entity = semantic_matches[0][0]
                    logger.info(
                        f"Found entity '{entity.name}' via semantic search "
                        f"(similarity: {semantic_matches[0][1]:.2f}) for query '{input.query}'"
                    )
            except Exception as e:
                logger.warning(f"Semantic search failed for '{input.query}': {e}")

        if not entity:
            # Build helpful suggestions
            suggestions = [
                "Try searching with a different entity name or description",
                "Check if the entity has been discovered (query a connector first)",
            ]

            # Try to provide semantic suggestions even when not finding an entity
            try:
                semantic_suggestions = await self._semantic_search(
                    query=input.query,
                    tenant_id=tenant_id,
                    limit=3,
                    min_similarity=0.4,  # Very low threshold for suggestions
                )
                if semantic_suggestions:
                    similar_names = [f"'{e.name}'" for e, _ in semantic_suggestions]
                    suggestions.insert(0, f"Did you mean: {', '.join(similar_names)}?")
            except Exception:  # noqa: S110 -- intentional silent exception handling
                pass  # Ignore errors for suggestions

            return LookupTopologyResult(
                found=False,
                suggestions=suggestions,
            )

        # Traverse topology
        chain = await self.repository.traverse_topology(
            start_entity_id=entity.id,
            max_depth=input.traverse_depth,
            cross_connectors=input.cross_connectors,
            tenant_id=tenant_id,
        )

        # Collect connectors traversed
        connectors: set[str] = set()
        for item in chain:
            if item.connector_id:
                connectors.add(str(item.connector_id))

        # Get confirmed SAME_AS entities (cross-connector correlations)
        same_as_entities: list[CorrelatedEntity] = []
        try:
            same_as_with_verification = await self.repository.get_same_as_entities(
                entity.id, tenant_id=tenant_id
            )
            for correlated_entity, verified_via in same_as_with_verification:
                same_as_entities.append(
                    CorrelatedEntity(
                        entity=TopologyEntity.model_validate(correlated_entity),
                        connector_type=correlated_entity.connector_type or "unknown",
                        connector_name=correlated_entity.connector_name,
                        verified_via=verified_via,
                    )
                )
        except Exception as e:
            # Log but don't fail - SAME_AS lookup is optional
            logger.warning(f"Failed to get SAME_AS entities for {entity.name}: {e}")

        # Find possibly related entities via embedding similarity
        possibly_related: list[PossiblyRelatedEntity] = []
        try:
            possibly_related = await self.correlation_service.find_possibly_related(
                entity=entity,
                min_similarity=0.7,
                max_results=5,
            )
        except Exception as e:
            # Log but don't fail - correlation is optional
            logger.warning(f"Failed to find related entities for {entity.name}: {e}")

        return LookupTopologyResult(
            found=True,
            entity=TopologyEntity.model_validate(entity),
            topology_chain=chain,
            connectors_traversed=list(connectors),
            same_as_entities=same_as_entities,
            possibly_related=possibly_related,
        )

    async def _semantic_search(
        self,
        query: str,
        tenant_id: str,
        limit: int = 5,
        min_similarity: float = 0.6,
    ) -> list[tuple[TopologyEntityModel, float]]:
        """
        Search for entities using semantic similarity.

        Generates an embedding for the query and finds entities with
        similar embeddings. This handles:
        - Partial name matches ("bgodds-service" → "bgoddsservice")
        - Natural language queries ("the web service in production")
        - Spelling variations and typos

        Args:
            query: Search query (entity name or description)
            tenant_id: Tenant ID
            limit: Maximum results
            min_similarity: Minimum similarity threshold (0-1)

        Returns:
            List of (entity, similarity) tuples sorted by similarity descending
        """
        # Generate embedding for the query
        query_embedding = await self.embedding_service.generate_embedding(query)

        # Search for similar entities
        return await self.repository.find_similar_entities(
            embedding=query_embedding,
            tenant_id=tenant_id,
            limit=limit,
            min_similarity=min_similarity,
        )

    # =========================================================================
    # Invalidation
    # =========================================================================

    async def invalidate(
        self,
        input: InvalidateTopologyInput,
        tenant_id: str,
    ) -> InvalidateTopologyResult:
        """
        Invalidate a stale entity.

        Called when the agent detects that stored topology no longer
        matches reality (e.g., 404 from API).
        """
        entity = await self.repository.get_entity_by_name(
            name=input.entity_name,
            tenant_id=tenant_id,
        )

        if not entity:
            return InvalidateTopologyResult(
                invalidated=False,
                message=f"Entity '{input.entity_name}' not found.",
            )

        try:
            relationships_affected = await self.repository.mark_entity_stale(entity.id)
            await self.session.commit()

            logger.info(
                f"Invalidated entity {input.entity_name}: {input.reason}. "
                f"Affected {relationships_affected} relationships."
            )

            return InvalidateTopologyResult(
                invalidated=True,
                entities_affected=1,
                relationships_affected=relationships_affected,
                message=f"Invalidated {input.entity_name}. Will re-investigate next time.",
            )

        except Exception as e:
            await self.session.rollback()
            logger.error(f"Failed to invalidate {input.entity_name}: {e}")
            return InvalidateTopologyResult(
                invalidated=False,
                message=f"Failed to invalidate: {e!s}",
            )

    # =========================================================================
    # Maintenance
    # =========================================================================

    async def cleanup_stale_entities(
        self,
        retention_days: int = 30,
        tenant_id: str | None = None,
    ) -> int:
        """
        Delete entities that have been stale for longer than retention period.

        Args:
            retention_days: Only delete entities stale for longer than this.
            tenant_id: Scope deletion to this tenant only (security).

        Returns the number of entities deleted.
        """
        older_than = datetime.now(tz=UTC) - timedelta(days=retention_days)
        count = await self.repository.delete_stale_entities(older_than, tenant_id=tenant_id)
        await self.session.commit()
        logger.info(f"Cleaned up {count} stale entities older than {retention_days} days")
        return count

    async def delete_entities_for_connector(self, connector_id: UUID) -> int:
        """
        Delete all topology entities for a given connector.

        Called when a connector is deleted to prevent orphaned topology data.
        The database's ON DELETE CASCADE will handle related records:
        - topology_embeddings
        - topology_relationships
        - topology_same_as

        Returns the number of entities deleted.
        """
        count = await self.repository.delete_entities_by_connector(connector_id)
        await self.session.commit()
        logger.info(f"Deleted {count} topology entities for connector {connector_id}")
        return count

    async def cleanup_orphaned_entities(self, valid_connector_ids: list[UUID]) -> int:
        """
        Delete topology entities whose connectors no longer exist.

        Used for one-time cleanup of orphaned entities.

        Returns the number of entities deleted.
        """
        count = await self.repository.delete_orphaned_entities(valid_connector_ids)
        await self.session.commit()
        logger.info(f"Cleaned up {count} orphaned topology entities")
        return count


# =============================================================================
# Dependency Injection
# =============================================================================


async def get_topology_service(session: AsyncSession) -> TopologyService:
    """Get a TopologyService instance for dependency injection."""
    # Note: EmbeddingService will be injected in Phase 2
    return TopologyService(session)
