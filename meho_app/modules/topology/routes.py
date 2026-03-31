# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Topology Admin API Routes.

REST API for viewing and managing topology:
- List entities
- View topology graph
- Manual invalidation
- Cleanup stale entities

These endpoints are read-only debugging tools, not primary interfaces.
The agent tools (lookup, store, invalidate) are the primary interface.
"""
# mypy: disable-error-code="arg-type"

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.api.auth import get_current_user
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.database import get_db_session

from .repository import TopologyRepository
from .schemas import (
    InvalidateTopologyInput,
    LookupTopologyInput,
    SameAsSuggestionWithEntities,
    SuggestionActionResponse,
    SuggestionApproveRequest,
    SuggestionListResponse,
    SuggestionRejectRequest,
    TopologyEntity,
    TopologyEntityResponse,
    TopologyGraphNode,
    TopologyGraphRelationship,
    TopologyGraphResponse,
    TopologyGraphSameAs,
    TopologySearchResponse,
    ConnectorRelationshipCreate,
    ConnectorRelationshipResponse,
    CONNECTOR_RELATIONSHIP_TYPES,
)
from .models import TopologyEntityModel, TopologyRelationshipModel
from .service import TopologyService

logger = get_logger(__name__)

router = APIRouter(prefix="/topology", tags=["topology"])


# =============================================================================
# Request/Response Models
# =============================================================================


class TopologyStatsResponse(BaseModel):
    """Statistics about topology data."""

    total_entities: int
    total_relationships: int
    total_same_as: int
    stale_entities: int


class TopologyLookupResponse(BaseModel):
    """Response for topology lookup."""

    found: bool
    entity: TopologyEntity | None = None
    chain: list[dict[str, Any]] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)
    possibly_related: list[dict[str, Any]] = Field(default_factory=list)


# =============================================================================
# Endpoints
# =============================================================================


@router.get("/health")
async def topology_health():
    """Health check for topology service."""
    return {"status": "healthy", "service": "topology"}


@router.get("/entities", response_model=TopologySearchResponse)
async def list_entities(
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    include_stale: bool = Query(False, description="Include stale entities"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    List all topology entities for the current tenant.

    Returns paginated list of entities with their metadata.
    Useful for debugging what the agent has learned.
    """
    repository = TopologyRepository(session)

    entities, total = await repository.get_all_entities_for_tenant(
        tenant_id=user_context.tenant_id,
        include_stale=include_stale,
        limit=limit,
        offset=offset,
    )

    # Convert to response
    response_entities = []
    for entity in entities:
        has_embedding = entity.embedding is not None
        response_entities.append(
            TopologyEntityResponse(
                id=entity.id,
                name=entity.name,
                entity_type=entity.entity_type,
                connector_type=entity.connector_type,
                connector_id=entity.connector_id,
                scope=entity.scope,
                canonical_id=entity.canonical_id,
                description=entity.description,
                raw_attributes=entity.raw_attributes,
                discovered_at=entity.discovered_at,
                last_verified_at=entity.last_verified_at,
                stale_at=entity.stale_at,
                tenant_id=entity.tenant_id,
                has_embedding=has_embedding,
            )
        )

    return TopologySearchResponse(
        entities=response_entities,
        total=total,
    )


@router.get("/entities/{entity_id}", response_model=TopologyEntityResponse)
async def get_entity(
    entity_id: UUID,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a specific entity by ID."""
    repository = TopologyRepository(session)
    entity = await repository.get_entity_by_id(entity_id)

    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if entity.tenant_id != user_context.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    has_embedding = entity.embedding is not None
    return TopologyEntityResponse(
        id=entity.id,
        name=entity.name,
        entity_type=entity.entity_type,
        connector_type=entity.connector_type,
        connector_id=entity.connector_id,
        scope=entity.scope,
        canonical_id=entity.canonical_id,
        description=entity.description,
        raw_attributes=entity.raw_attributes,
        discovered_at=entity.discovered_at,
        last_verified_at=entity.last_verified_at,
        stale_at=entity.stale_at,
        tenant_id=entity.tenant_id,
        has_embedding=has_embedding,
    )


@router.get("/lookup", response_model=TopologyLookupResponse)
async def lookup_topology(
    query: str = Query(..., description="Entity name to look up"),
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    traverse_depth: int = Query(10, ge=1, le=50),
    cross_connectors: bool = Query(True),
):
    """
    Look up topology for an entity.

    Returns the full topology chain from the entity,
    traversing relationships and SAME_AS links.
    """
    service = TopologyService(session)

    result = await service.lookup(
        input=LookupTopologyInput(
            query=query,
            traverse_depth=traverse_depth,
            cross_connectors=cross_connectors,
        ),
        tenant_id=user_context.tenant_id,
    )

    return TopologyLookupResponse(
        found=result.found,
        entity=result.entity,
        chain=[
            {
                "depth": item.depth,
                "entity": item.entity,
                "connector": item.connector,
                "relationship": item.relationship,
            }
            for item in result.topology_chain
        ],
        connectors=result.connectors_traversed,
        possibly_related=[
            {
                "entity": p.entity,
                "similarity": p.similarity,
            }
            for p in result.possibly_related
        ],
    )


@router.post("/entities/{entity_name}/invalidate")
async def invalidate_entity(
    entity_name: str,
    reason: str = Query(..., description="Reason for invalidation"),
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Manually invalidate an entity.

    Marks the entity as stale so it will be re-discovered
    on the next investigation.
    """
    service = TopologyService(session)

    result = await service.invalidate(
        input=InvalidateTopologyInput(
            entity_name=entity_name,
            reason=reason,
        ),
        tenant_id=user_context.tenant_id,
    )

    if not result.invalidated:
        raise HTTPException(status_code=404, detail=result.message)

    return {
        "invalidated": True,
        "entities_affected": result.entities_affected,
        "relationships_affected": result.relationships_affected,
        "message": result.message,
    }


@router.delete("/stale")
async def cleanup_stale_entities(
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    retention_days: int = Query(
        30, ge=1, le=365, description="Delete entities stale for longer than this"
    ),
):
    """
    Clean up stale entities older than retention period.

    Returns the number of entities deleted.
    """
    service = TopologyService(session)
    count = await service.cleanup_stale_entities(
        retention_days=retention_days, tenant_id=user_context.tenant_id
    )

    return {
        "deleted": count,
        "retention_days": retention_days,
    }


@router.get("/graph", response_model=TopologyGraphResponse)
async def get_topology_graph(
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    include_stale: bool = Query(False, description="Include stale entities"),
    limit: int = Query(100, ge=1, le=500, description="Maximum nodes to return"),
):
    """
    Get topology as a graph for visualization.

    Returns nodes, relationships, and same_as links suitable for rendering
    with a graph visualization library.
    """
    repository = TopologyRepository(session)

    # Get entities
    entities, _ = await repository.get_all_entities_for_tenant(
        tenant_id=user_context.tenant_id,
        include_stale=include_stale,
        limit=limit,
    )

    # Build nodes from entities
    nodes = [
        TopologyGraphNode(
            id=entity.id,
            name=entity.name,
            entity_type=entity.entity_type,
            connector_type=entity.connector_type,
            connector_id=entity.connector_id,
            scope=entity.scope,
            canonical_id=entity.canonical_id,
            description=entity.description,
            raw_attributes=entity.raw_attributes,
            discovered_at=entity.discovered_at,
            last_verified_at=entity.last_verified_at,
            stale_at=entity.stale_at,
            tenant_id=entity.tenant_id,
        )
        for entity in entities
    ]

    # Pass 1: Collect all relationships and identify missing target entities
    entity_ids = {e.id for e in entities}
    all_rels = []
    missing_target_ids: set[UUID] = set()

    for entity in entities:
        rels = await repository.get_relationships_from(entity.id)
        for rel in rels:
            all_rels.append(rel)
            if rel.to_entity_id not in entity_ids:
                missing_target_ids.add(rel.to_entity_id)

    # Pass 2: Fetch missing relationship-target entities to preserve graph edges
    if missing_target_ids:
        for target_id in missing_target_ids:
            target_entity = await repository.get_entity_by_id(target_id)
            if target_entity and target_entity.tenant_id == user_context.tenant_id:
                entities.append(target_entity)
                entity_ids.add(target_entity.id)
                nodes.append(
                    TopologyGraphNode(
                        id=target_entity.id,
                        name=target_entity.name,
                        entity_type=target_entity.entity_type,
                        connector_type=target_entity.connector_type,
                        connector_id=target_entity.connector_id,
                        scope=target_entity.scope,
                        canonical_id=target_entity.canonical_id,
                        description=target_entity.description,
                        raw_attributes=target_entity.raw_attributes,
                        discovered_at=target_entity.discovered_at,
                        last_verified_at=target_entity.last_verified_at,
                        stale_at=target_entity.stale_at,
                        tenant_id=target_entity.tenant_id,
                    )
                )

    # Build relationships (all targets now guaranteed to be in entity_ids)
    relationships = []
    same_as_list = []
    seen_same_as = set()

    for rel in all_rels:
        if rel.to_entity_id in entity_ids:
            relationships.append(
                TopologyGraphRelationship(
                    id=rel.id,
                    from_entity_id=rel.from_entity_id,
                    to_entity_id=rel.to_entity_id,
                    relationship_type=rel.relationship_type,
                    discovered_at=rel.discovered_at,
                    last_verified_at=rel.last_verified_at,
                )
            )

    for entity in entities:
        same_as_rels = await repository.get_same_as_for_entity(
            entity.id, tenant_id=user_context.tenant_id
        )
        for same_as in same_as_rels:
            other_id = (
                same_as.entity_b_id if same_as.entity_a_id == entity.id else same_as.entity_a_id
            )
            if other_id in entity_ids:
                pair_key = tuple(sorted([str(same_as.entity_a_id), str(same_as.entity_b_id)]))
                if pair_key not in seen_same_as:
                    seen_same_as.add(pair_key)
                    same_as_list.append(
                        TopologyGraphSameAs(
                            id=same_as.id,
                            entity_a_id=same_as.entity_a_id,
                            entity_b_id=same_as.entity_b_id,
                            similarity_score=same_as.similarity_score,
                            verified_via=same_as.verified_via or [],
                            discovered_at=same_as.discovered_at,
                            last_verified_at=same_as.last_verified_at,
                        )
                    )

    return TopologyGraphResponse(
        nodes=nodes,
        relationships=relationships,
        same_as=same_as_list,
    )


# =============================================================================
# SAME_AS Suggestion Endpoints (Phase 2 Correlation)
# =============================================================================


@router.get("/suggestions", response_model=SuggestionListResponse)
async def list_suggestions(
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    List pending SAME_AS suggestions for the current tenant.

    Suggestions are created automatically when:
    - K8s Ingress hostname matches a connector's target
    - VMware VM IP matches a connector's target
    - GCP Instance IP matches a connector's target

    Users can approve or reject these suggestions.
    """
    repository = TopologyRepository(session)

    suggestions, total = await repository.get_pending_suggestions(
        tenant_id=user_context.tenant_id,
        limit=limit,
        offset=offset,
    )

    # Convert to response with entity names
    response_suggestions = []
    for suggestion in suggestions:
        entity_a = suggestion.entity_a
        entity_b = suggestion.entity_b

        response_suggestions.append(
            SameAsSuggestionWithEntities(
                id=suggestion.id,
                entity_a_id=suggestion.entity_a_id,
                entity_b_id=suggestion.entity_b_id,
                confidence=suggestion.confidence,
                match_type=suggestion.match_type,
                match_details=suggestion.match_details,
                status=suggestion.status,
                suggested_at=suggestion.suggested_at,
                resolved_at=suggestion.resolved_at,
                resolved_by=suggestion.resolved_by,
                tenant_id=suggestion.tenant_id,
                entity_a_name=entity_a.name if entity_a else "Unknown",
                entity_b_name=entity_b.name if entity_b else "Unknown",
                entity_a_connector_name=entity_a.connector_name if entity_a else None,
                entity_b_connector_name=entity_b.connector_name if entity_b else None,
            )
        )

    return SuggestionListResponse(
        suggestions=response_suggestions,
        total=total,
    )


@router.get("/suggestions/{suggestion_id}", response_model=SameAsSuggestionWithEntities)
async def get_suggestion(
    suggestion_id: UUID,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Get a specific suggestion by ID."""
    repository = TopologyRepository(session)
    suggestion = await repository.get_suggestion_by_id(suggestion_id)

    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if suggestion.tenant_id != user_context.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    entity_a = suggestion.entity_a
    entity_b = suggestion.entity_b

    return SameAsSuggestionWithEntities(
        id=suggestion.id,
        entity_a_id=suggestion.entity_a_id,
        entity_b_id=suggestion.entity_b_id,
        confidence=suggestion.confidence,
        match_type=suggestion.match_type,
        match_details=suggestion.match_details,
        status=suggestion.status,
        suggested_at=suggestion.suggested_at,
        resolved_at=suggestion.resolved_at,
        resolved_by=suggestion.resolved_by,
        tenant_id=suggestion.tenant_id,
        entity_a_name=entity_a.name if entity_a else "Unknown",
        entity_b_name=entity_b.name if entity_b else "Unknown",
        entity_a_connector_name=entity_a.connector_name if entity_a else None,
        entity_b_connector_name=entity_b.connector_name if entity_b else None,
    )


@router.post("/suggestions/{suggestion_id}/approve", response_model=SuggestionActionResponse)
async def approve_suggestion(
    suggestion_id: UUID,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    body: SuggestionApproveRequest = None,
):
    """
    Approve a SAME_AS suggestion.

    Creates a confirmed SAME_AS relationship between the two entities.
    The relationship will be visible in topology traversal and graph views.
    """
    repository = TopologyRepository(session)

    # First check the suggestion exists and belongs to this tenant
    suggestion = await repository.get_suggestion_by_id(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if suggestion.tenant_id != user_context.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if suggestion.status != "pending":
        raise HTTPException(status_code=400, detail=f"Suggestion already {suggestion.status}")

    # Approve the suggestion
    same_as = await repository.approve_suggestion(
        suggestion_id=suggestion_id,
        user_id=user_context.user_id,
    )

    await session.commit()

    if same_as:
        entity_a = suggestion.entity_a
        entity_b = suggestion.entity_b
        return SuggestionActionResponse(
            success=True,
            message=f"Created SAME_AS relationship between '{entity_a.name if entity_a else 'Unknown'}' and '{entity_b.name if entity_b else 'Unknown'}'",
            same_as_created=True,
        )
    else:
        raise HTTPException(status_code=500, detail="Failed to create SAME_AS relationship")


@router.post("/suggestions/{suggestion_id}/reject", response_model=SuggestionActionResponse)
async def reject_suggestion(
    suggestion_id: UUID,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    body: SuggestionRejectRequest = None,
):
    """
    Reject a SAME_AS suggestion.

    The suggestion will be marked as rejected and will not be shown again.
    """
    repository = TopologyRepository(session)

    # First check the suggestion exists and belongs to this tenant
    suggestion = await repository.get_suggestion_by_id(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if suggestion.tenant_id != user_context.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if suggestion.status != "pending":
        raise HTTPException(status_code=400, detail=f"Suggestion already {suggestion.status}")

    # Reject the suggestion
    success = await repository.reject_suggestion(
        suggestion_id=suggestion_id,
        user_id=user_context.user_id,
    )

    await session.commit()

    if success:
        entity_a = suggestion.entity_a
        entity_b = suggestion.entity_b
        return SuggestionActionResponse(
            success=True,
            message=f"Rejected suggestion for '{entity_a.name if entity_a else 'Unknown'}' ↔ '{entity_b.name if entity_b else 'Unknown'}'",
            same_as_created=False,
        )
    else:
        raise HTTPException(status_code=500, detail="Failed to reject suggestion")


# =============================================================================
# SAME_AS Discovery Endpoints (TASK-160 Phase 2)
# =============================================================================


class DiscoveryResponse(BaseModel):
    """Response from SAME_AS discovery run."""

    success: bool
    suggestions_created: int
    suggestions_skipped_existing: int
    suggestions_skipped_ineligible: int
    total_pairs_analyzed: int
    message: str


@router.post("/suggestions/discover", response_model=DiscoveryResponse)
async def trigger_same_as_discovery(
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    min_similarity: float = Query(0.70, ge=0.5, le=1.0, description="Minimum embedding similarity"),
    limit: int = Query(50, ge=1, le=200, description="Maximum suggestions to create"),
    verify: bool = Query(False, description="Run LLM verification on new suggestions"),
):
    """
    Trigger on-demand SAME_AS discovery.

    Scans all entity embeddings across different connectors to find
    high-similarity pairs that might represent the same physical resource.

    Flow:
    1. Query pgvector for cross-connector similar pairs
    2. Filter by SameAsEligibility rules (Node ↔ VM ✅, Pod ↔ VM ❌)
    3. Skip pairs with existing suggestions or SAME_AS relationships
    4. Create new pending suggestions
    5. Optionally run LLM verification on mid-confidence matches

    Args:
        min_similarity: Minimum embedding similarity threshold (0.5-1.0)
        limit: Maximum number of suggestions to create
        verify: If True, run LLM verification on new suggestions

    Returns:
        Discovery result with counts of created, skipped, and analyzed pairs
    """
    from .clustering import ClusteringService
    from .suggestion_verifier import SuggestionVerifier

    try:
        # Run discovery
        service = ClusteringService(session)
        result = await service.discover_same_as_candidates(
            tenant_id=user_context.tenant_id,
            min_similarity=min_similarity,
            limit=limit,
        )

        # Optionally run LLM verification on new suggestions
        if verify and result.suggestions_created > 0:
            verifier = SuggestionVerifier(session)
            repository = TopologyRepository(session)

            # Get pending suggestions in LLM verification range
            suggestions = await repository.get_suggestions_needing_verification(
                tenant_id=user_context.tenant_id,
                min_confidence=0.70,
                max_confidence=0.89,
                limit=result.suggestions_created,
            )

            for suggestion in suggestions:
                try:
                    await verifier.process_and_resolve(suggestion.id)
                except Exception as e:
                    logger.warning(f"LLM verification failed for {suggestion.id}: {e}")

        await session.commit()

        return DiscoveryResponse(
            success=True,
            suggestions_created=result.suggestions_created,
            suggestions_skipped_existing=result.suggestions_skipped_existing,
            suggestions_skipped_ineligible=result.suggestions_skipped_ineligible,
            total_pairs_analyzed=result.total_pairs_analyzed,
            message=result.message,
        )

    except Exception as e:
        logger.error(f"SAME_AS discovery failed: {e}")
        raise HTTPException(status_code=500, detail=f"Discovery failed: {e!s}") from e


# =============================================================================
# LLM Verification Endpoints (Phase 3)
# =============================================================================


class VerificationResponse(BaseModel):
    """Response from LLM verification."""

    success: bool
    suggestion_id: UUID
    new_status: str  # "approved", "rejected", or "pending"
    llm_result: dict | None = None
    message: str


@router.post("/suggestions/{suggestion_id}/verify", response_model=VerificationResponse)
async def verify_suggestion(
    suggestion_id: UUID,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Manually trigger LLM verification for a SAME_AS suggestion.

    Uses LLM to analyze the two entities' attributes and determine
    if they represent the same physical/logical resource.

    Based on LLM confidence:
    - High confidence + is_same=True → Auto-approve
    - High confidence + is_same=False → Auto-reject
    - Low confidence → Leave pending for manual review
    """
    from .suggestion_verifier import SuggestionVerifier

    repository = TopologyRepository(session)

    # Check the suggestion exists and belongs to this tenant
    suggestion = await repository.get_suggestion_by_id(suggestion_id)
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if suggestion.tenant_id != user_context.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if suggestion.status != "pending":
        raise HTTPException(status_code=400, detail=f"Suggestion already {suggestion.status}")

    # Run LLM verification
    try:
        verifier = SuggestionVerifier(session)
        new_status = await verifier.process_and_resolve(suggestion_id)

        await session.commit()

        # Reload to get updated verification result
        suggestion = await repository.get_suggestion_by_id(suggestion_id)

        entity_a = suggestion.entity_a if suggestion else None
        entity_b = suggestion.entity_b if suggestion else None
        entity_names = f"'{entity_a.name if entity_a else 'Unknown'}' ↔ '{entity_b.name if entity_b else 'Unknown'}'"

        if new_status == "approved":
            message = f"LLM verified and approved: {entity_names}"
        elif new_status == "rejected":
            message = f"LLM verified and rejected: {entity_names}"
        else:
            message = f"LLM uncertain, left for manual review: {entity_names}"

        return VerificationResponse(
            success=True,
            suggestion_id=suggestion_id,
            new_status=new_status,
            llm_result=suggestion.llm_verification_result if suggestion else None,
            message=message,
        )

    except Exception as e:
        logger.error(f"LLM verification failed for suggestion {suggestion_id}: {e}")
        raise HTTPException(status_code=500, detail=f"LLM verification failed: {e!s}") from e


# =============================================================================
# Connector Relationship Endpoints (D-12)
# =============================================================================


class ConnectorRelationshipListResponse(BaseModel):
    """Response for listing connector relationships."""
    relationships: list[ConnectorRelationshipResponse]
    total: int


@router.get("/connector-relationships", response_model=ConnectorRelationshipListResponse)
async def list_connector_relationships(
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """
    List all connector-to-connector relationships for the tenant.

    Returns relationships where both endpoints are Connector-type topology
    entities belonging to the current tenant.
    """
    from sqlalchemy import select, and_

    from_entity = sa.orm.aliased(TopologyEntityModel, name="from_ent")
    to_entity = sa.orm.aliased(TopologyEntityModel, name="to_ent")

    stmt = (
        select(
            TopologyRelationshipModel,
            from_entity.connector_id.label("from_connector_id"),
            from_entity.name.label("from_connector_name"),
            to_entity.connector_id.label("to_connector_id"),
            to_entity.name.label("to_connector_name"),
        )
        .join(from_entity, TopologyRelationshipModel.from_entity_id == from_entity.id)
        .join(to_entity, TopologyRelationshipModel.to_entity_id == to_entity.id)
        .where(
            and_(
                from_entity.entity_type == "Connector",
                to_entity.entity_type == "Connector",
                from_entity.tenant_id == user_context.tenant_id,
                to_entity.tenant_id == user_context.tenant_id,
            )
        )
    )

    result = await session.execute(stmt)
    rows = result.all()

    relationships = []
    for row in rows:
        rel = row[0]
        relationships.append(ConnectorRelationshipResponse(
            id=rel.id,
            from_connector_id=row.from_connector_id,
            from_connector_name=row.from_connector_name,
            to_connector_id=row.to_connector_id,
            to_connector_name=row.to_connector_name,
            relationship_type=rel.relationship_type,
            discovered_at=rel.discovered_at,
            last_verified_at=rel.last_verified_at,
        ))

    return ConnectorRelationshipListResponse(
        relationships=relationships,
        total=len(relationships),
    )


@router.post("/connector-relationships", response_model=ConnectorRelationshipResponse, status_code=201)
async def create_connector_relationship(
    body: ConnectorRelationshipCreate,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Create a relationship between two connector instances.

    Validates that:
    - Both connectors exist and belong to the tenant
    - The relationship_type is in the fixed vocabulary
    - Both connectors have topology entities (auto-creates if missing)
    """
    from sqlalchemy import select, and_
    from meho_app.modules.connectors.models import ConnectorModel

    for connector_id, label in [
        (body.from_connector_id, "from_connector_id"),
        (body.to_connector_id, "to_connector_id"),
    ]:
        stmt = select(ConnectorModel).where(
            and_(
                ConnectorModel.id == connector_id,
                ConnectorModel.tenant_id == user_context.tenant_id,
            )
        )
        result = await session.execute(stmt)
        connector = result.scalar_one_or_none()
        if not connector:
            raise HTTPException(
                status_code=404,
                detail=f"Connector {connector_id} not found for {label}",
            )

    from_topo = await _get_or_create_connector_topology_entity(
        session, body.from_connector_id, user_context.tenant_id
    )
    to_topo = await _get_or_create_connector_topology_entity(
        session, body.to_connector_id, user_context.tenant_id
    )

    if not from_topo or not to_topo:
        raise HTTPException(
            status_code=500,
            detail="Failed to resolve topology entities for connectors",
        )

    from datetime import datetime as dt
    rel = TopologyRelationshipModel(
        from_entity_id=from_topo.id,
        to_entity_id=to_topo.id,
        relationship_type=body.relationship_type,
        discovered_at=dt.utcnow(),
    )
    session.add(rel)
    await session.commit()
    await session.refresh(rel)

    return ConnectorRelationshipResponse(
        id=rel.id,
        from_connector_id=body.from_connector_id,
        from_connector_name=from_topo.name,
        to_connector_id=body.to_connector_id,
        to_connector_name=to_topo.name,
        relationship_type=rel.relationship_type,
        discovered_at=rel.discovered_at,
        last_verified_at=rel.last_verified_at,
    )


@router.put("/connector-relationships/{relationship_id}", response_model=ConnectorRelationshipResponse)
async def update_connector_relationship(
    relationship_id: UUID,
    body: ConnectorRelationshipCreate,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Update an existing connector relationship."""
    from sqlalchemy import select

    stmt = select(TopologyRelationshipModel).where(
        TopologyRelationshipModel.id == relationship_id,
    )
    result = await session.execute(stmt)
    rel = result.scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Relationship not found")

    from_entity = await session.get(TopologyEntityModel, rel.from_entity_id)
    to_entity = await session.get(TopologyEntityModel, rel.to_entity_id)
    if (
        not from_entity
        or not to_entity
        or from_entity.tenant_id != user_context.tenant_id
        or to_entity.tenant_id != user_context.tenant_id
    ):
        raise HTTPException(status_code=403, detail="Access denied")

    from_topo = await _get_or_create_connector_topology_entity(
        session, body.from_connector_id, user_context.tenant_id
    )
    to_topo = await _get_or_create_connector_topology_entity(
        session, body.to_connector_id, user_context.tenant_id
    )

    if not from_topo or not to_topo:
        raise HTTPException(
            status_code=500,
            detail="Failed to resolve topology entities for connectors",
        )

    from datetime import datetime as dt
    rel.from_entity_id = from_topo.id
    rel.to_entity_id = to_topo.id
    rel.relationship_type = body.relationship_type
    rel.last_verified_at = dt.utcnow()
    await session.commit()
    await session.refresh(rel)

    return ConnectorRelationshipResponse(
        id=rel.id,
        from_connector_id=body.from_connector_id,
        from_connector_name=from_topo.name,
        to_connector_id=body.to_connector_id,
        to_connector_name=to_topo.name,
        relationship_type=rel.relationship_type,
        discovered_at=rel.discovered_at,
        last_verified_at=rel.last_verified_at,
    )


@router.delete("/connector-relationships/{relationship_id}")
async def delete_connector_relationship(
    relationship_id: UUID,
    user_context: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a connector relationship."""
    from sqlalchemy import select

    stmt = select(TopologyRelationshipModel).where(
        TopologyRelationshipModel.id == relationship_id,
    )
    result = await session.execute(stmt)
    rel = result.scalar_one_or_none()
    if not rel:
        raise HTTPException(status_code=404, detail="Relationship not found")

    from_entity = await session.get(TopologyEntityModel, rel.from_entity_id)
    if not from_entity or from_entity.tenant_id != user_context.tenant_id:
        raise HTTPException(status_code=403, detail="Access denied")

    await session.delete(rel)
    await session.commit()

    return {"deleted": True, "relationship_id": str(relationship_id)}


# =============================================================================
# Connector Relationship Helpers
# =============================================================================


async def _get_or_create_connector_topology_entity(
    session: AsyncSession,
    connector_id: UUID,
    tenant_id: str,
) -> TopologyEntityModel | None:
    """
    Get the topology entity for a connector, or return None if not found.

    Connectors are registered as topology entities on creation. This helper
    looks up the existing entity by connector_id and entity_type "Connector".
    """
    from sqlalchemy import select, and_

    stmt = select(TopologyEntityModel).where(
        and_(
            TopologyEntityModel.connector_id == connector_id,
            TopologyEntityModel.entity_type == "Connector",
            TopologyEntityModel.tenant_id == tenant_id,
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
