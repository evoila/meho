# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector memory operations.

CRUD, search, and bulk endpoints for connector-scoped memories.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission
from meho_app.modules.memory.models import ConfidenceLevel, MemoryType
from meho_app.modules.memory.schemas import (
    BulkCreateMemoriesRequest,
    BulkCreateMemoriesResponse,
    MemoryCreate,
    MemoryFilter,
    MemoryResponse,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryUpdate,
)

logger = get_logger(__name__)

MSG_MEMORY_NOT_FOUND = "Memory not found"

router = APIRouter()


# ---------------------------------------------------------------------------
# Request/response helpers
# ---------------------------------------------------------------------------


class BulkDeleteRequest(BaseModel):
    """Request body for bulk memory deletion."""

    memory_ids: list[str] = Field(..., min_length=1, description="IDs of memories to delete")


class DeleteResponse(BaseModel):
    """Response for single deletion."""

    deleted: bool


class BulkDeleteResponse(BaseModel):
    """Response for bulk deletion."""

    deleted: int


# ---------------------------------------------------------------------------
# Helper: verify connector belongs to tenant
# ---------------------------------------------------------------------------


async def _verify_connector(session, connector_id: str, tenant_id: str):
    """Verify connector exists and belongs to the user's tenant. Raises 404 if not found."""
    import uuid

    from sqlalchemy import select

    from meho_app.modules.connectors.models import ConnectorModel

    query = select(ConnectorModel).where(
        ConnectorModel.id == uuid.UUID(connector_id),
        ConnectorModel.tenant_id == tenant_id,
    )
    result = await session.execute(query)
    connector = result.scalar_one_or_none()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    return connector


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/memories",
    response_model=MemoryResponse,
    status_code=201,
    responses={500: {"description": "Internal error: ..."}},
)
async def create_memory(
    connector_id: str,
    memory_create: MemoryCreate,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Create a single memory for a connector.

    Server-side enforces connector_id and tenant_id from path/auth context
    to prevent scope escalation.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            # Override client-provided scoping fields
            if user.tenant_id is None:
                raise HTTPException(status_code=403, detail="Tenant context required")
            memory_create.connector_id = connector_id
            memory_create.tenant_id = user.tenant_id

            service = get_memory_service(session)
            result = await service.create_with_dedup(memory_create)
            await session.commit()

            logger.info(
                "memory_created",
                connector_id=connector_id,
                memory_id=result.id,
                memory_type=result.memory_type,
                merged=result.merged,
            )

            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating memory for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.get(
    "/{connector_id}/memories",
    response_model=list[MemoryResponse],
    responses={500: {"description": "Internal error: ..."}},
)
async def list_memories(
    connector_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
    memory_type: Annotated[MemoryType | None, Query(description="Filter by memory type")] = None,
    confidence_level: Annotated[
        ConfidenceLevel | None, Query(description="Filter by confidence level")
    ] = None,
    created_after: Annotated[
        datetime | None, Query(description="Filter: created after this date")
    ] = None,
    created_before: Annotated[
        datetime | None, Query(description="Filter: created before this date")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000, description="Max results")] = 100,
    offset: Annotated[int, Query(ge=0, description="Pagination offset")] = 0,
):
    """
    List memories for a connector with optional filters.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            filter_params = MemoryFilter(
                connector_id=connector_id,
                tenant_id=user.tenant_id,
                memory_type=memory_type,
                confidence_level=confidence_level,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
                offset=offset,
            )

            service = get_memory_service(session)
            return await service.list_memories(filter_params)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing memories for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.get(
    "/{connector_id}/memories/{memory_id}",
    response_model=MemoryResponse,
    responses={
        404: {"description": "Memory not found"},
        500: {"description": "Internal error: ..."},
    },
)
async def get_memory(
    connector_id: str,
    memory_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
):
    """
    Get a single memory by ID.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            service = get_memory_service(session)
            result = await service.get_memory(memory_id, user.tenant_id)

            if result is None:
                raise HTTPException(status_code=404, detail=MSG_MEMORY_NOT_FOUND)

            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting memory {memory_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.patch(
    "/{connector_id}/memories/{memory_id}",
    response_model=MemoryResponse,
    responses={
        404: {"description": "Memory not found"},
        500: {"description": "Internal error: ..."},
    },
)
async def update_memory(
    connector_id: str,
    memory_id: str,
    updates: MemoryUpdate,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Update a memory (PATCH semantics - all fields optional).
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            service = get_memory_service(session)
            result = await service.update_memory(memory_id, user.tenant_id, updates)

            if result is None:
                raise HTTPException(status_code=404, detail=MSG_MEMORY_NOT_FOUND)

            await session.commit()

            logger.info(
                "memory_updated",
                connector_id=connector_id,
                memory_id=memory_id,
            )

            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating memory {memory_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.delete(
    "/{connector_id}/memories/{memory_id}",
    response_model=DeleteResponse,
    responses={
        404: {"description": "Memory not found"},
        500: {"description": "Internal error: ..."},
    },
)
async def delete_memory(
    connector_id: str,
    memory_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Delete a single memory.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            service = get_memory_service(session)
            deleted = await service.delete_memory(memory_id, user.tenant_id)

            if not deleted:
                raise HTTPException(status_code=404, detail=MSG_MEMORY_NOT_FOUND)

            await session.commit()

            logger.info(
                "memory_deleted",
                connector_id=connector_id,
                memory_id=memory_id,
            )

            return DeleteResponse(deleted=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting memory {memory_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/memories/search",
    response_model=list[MemorySearchResult],
    responses={500: {"description": "Internal error: ..."}},
)
async def search_memories(
    connector_id: str,
    request: MemorySearchRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_READ))],
):
    """
    Semantic search across connector memories with confidence-weighted ranking.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            # Convert enum values to strings for service layer
            memory_type_str = request.memory_type.value if request.memory_type else None
            confidence_str = request.confidence_level.value if request.confidence_level else None

            service = get_memory_service(session)
            return await service.search(
                query=request.query,
                connector_id=connector_id,
                tenant_id=user.tenant_id,
                top_k=request.top_k,
                score_threshold=request.score_threshold,
                memory_type=memory_type_str,
                confidence_level=confidence_str,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching memories for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


# ---------------------------------------------------------------------------
# Bulk endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{connector_id}/memories/bulk", response_model=BulkCreateMemoriesResponse, status_code=201
)
async def bulk_create_memories(
    connector_id: str,
    request: BulkCreateMemoriesRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Create multiple memories in a single request with per-memory deduplication.

    Used by the extraction pipeline for batch ingestion.
    Server-side enforces connector_id and tenant_id on every memory.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            # Override scoping on every memory to prevent scope escalation
            if user.tenant_id is None:
                raise HTTPException(status_code=403, detail="Tenant context required")
            for memory in request.memories:
                memory.connector_id = connector_id
                memory.tenant_id = user.tenant_id

            service = get_memory_service(session)
            result = await service.bulk_create(request.memories)
            await session.commit()

            logger.info(
                "memories_bulk_created",
                connector_id=connector_id,
                created=result.created,
                merged=result.merged,
            )

            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error bulk creating memories for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.delete(
    "/{connector_id}/memories/bulk",
    response_model=BulkDeleteResponse,
    responses={500: {"description": "Internal error: ..."}},
)
async def bulk_delete_memories(
    connector_id: str,
    request: BulkDeleteRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Delete multiple memories in a single request.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.memory.service import get_memory_service

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            await _verify_connector(session, connector_id, user.tenant_id)

            service = get_memory_service(session)
            count = await service.delete_memories_bulk(request.memory_ids, user.tenant_id)
            await session.commit()

            logger.info(
                "memories_bulk_deleted",
                connector_id=connector_id,
                deleted=count,
            )

            return BulkDeleteResponse(deleted=count)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error bulk deleting memories for connector {connector_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e
