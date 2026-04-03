# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector operations management.

Handles listing, syncing, and instance-level CRUD for connector operations.
Uses the operation inheritance resolver to return merged type-level +
instance override views with source field for frontend badge rendering.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    ConnectorOperationResponse,
    CreateCustomOperationRequest,
    OverrideOperationRequest,
    SyncOperationsResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

MSG_CONNECTOR_NOT_FOUND = "Connector not found"

router = APIRouter()


def _model_to_response(op) -> ConnectorOperationResponse:
    """Convert a ConnectorOperationModel to API response with source field."""
    return ConnectorOperationResponse(
        id=str(op.id),
        operation_id=op.operation_id,
        name=op.name,
        description=op.description,
        category=op.category,
        parameters=op.parameters or [],
        example=op.example,
        source=op.source,
        is_enabled=op.is_enabled,
        safety_level=op.safety_level,
    )


@router.get(
    "/{connector_id}/operations",
    response_model=list[ConnectorOperationResponse],
    responses={404: {"description": "Connector not found"}},
)
async def list_connector_operations(
    connector_id: str,
    user: Annotated[UserContext, Depends(get_current_user)],
    search: Annotated[
        str | None, Query(description="Search in operation names/descriptions")
    ] = None,
    category: Annotated[
        str | None, Query(description="Filter by category (compute, storage, network, etc.)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    """
    List resolved operations for a connector with inheritance applied.

    Returns the merged view of type-level + instance override operations.
    Each operation includes a `source` field for frontend badge rendering:
    - 'type' = inherited from connector type definition
    - 'custom' = instance-specific override or addition

    Supports:
    - Text search in operation names/descriptions
    - Category filtering (compute, storage, network, cluster, etc.)
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.services.operation_inheritance import resolve_operations

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        # Use inheritance resolver for merged view
        resolved = await resolve_operations(
            session=session,
            connector_id=UUID(connector_id),
            connector_type=connector.connector_type,
            tenant_id=user.tenant_id,
        )

        # Apply filters on the resolved list
        results = resolved
        if search:
            search_lower = search.lower()
            results = [
                op
                for op in results
                if (
                    search_lower in (op.name or "").lower()
                    or search_lower in (op.operation_id or "").lower()
                    or search_lower in (op.description or "").lower()
                    or search_lower in (op.search_content or "").lower()
                )
            ]
        if category:
            results = [op for op in results if op.category == category]

        # Apply limit
        results = results[:limit]

        return [_model_to_response(op) for op in results]


@router.post(
    "/{connector_id}/operations",
    response_model=ConnectorOperationResponse,
    status_code=201,
)
async def create_custom_operation(
    connector_id: str,
    request: CreateCustomOperationRequest,
    user: Annotated[UserContext, Depends(get_current_user)],
):
    """
    Add a custom instance-level operation.

    Creates a new operation with source='custom' for this connector instance only.
    This operation does not override any type-level operation -- it is purely
    instance-specific.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.services.operation_service import add_custom_operation

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        op = await add_custom_operation(
            session=session,
            connector_id=UUID(connector_id),
            tenant_id=user.tenant_id,
            operation_data=request.model_dump(),
        )

        await session.commit()

        return _model_to_response(op)


@router.put(
    "/{connector_id}/operations/{op_id}/override",
    response_model=ConnectorOperationResponse,
)
async def create_or_update_override(
    connector_id: str,
    op_id: str,
    request: OverrideOperationRequest,
    user: Annotated[UserContext, Depends(get_current_user)],
):
    """
    Create or update an instance override of a type-level operation.

    Copies the type-level operation, sets source='custom', applies the
    provided overrides. The type_operation_id links back to the original.
    If an override already exists, it is updated.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.services.operation_service import override_operation

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        try:
            op = await override_operation(
                session=session,
                connector_id=UUID(connector_id),
                type_operation_id=UUID(op_id),
                overrides={k: v for k, v in request.model_dump().items() if v is not None},
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

        await session.commit()

        return _model_to_response(op)


@router.delete(
    "/{connector_id}/operations/{op_id}/override",
    status_code=204,
)
async def reset_override(
    connector_id: str,
    op_id: str,
    user: Annotated[UserContext, Depends(get_current_user)],
):
    """
    Reset to type-level definition by deleting the custom override.

    Only deletes operations with source='custom'. Has no effect on
    type-level operations.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.services.operation_service import reset_operation

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        deleted = await reset_operation(
            session=session,
            connector_id=UUID(connector_id),
            operation_id=UUID(op_id),
        )

        if not deleted:
            raise HTTPException(
                status_code=404, detail="Custom override not found for this operation"
            )

        await session.commit()


@router.patch(
    "/{connector_id}/operations/{op_id}/toggle",
    response_model=ConnectorOperationResponse,
)
async def toggle_operation(
    connector_id: str,
    op_id: str,
    user: Annotated[UserContext, Depends(get_current_user)],
):
    """
    Toggle enable/disable for an operation on this instance.

    For type-level operations, creates a custom override with
    is_enabled_override toggled. For custom operations, toggles
    is_enabled directly.
    """
    from sqlalchemy import and_, select

    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.models import ConnectorOperationModel
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.services.operation_service import (
        disable_operation,
        reset_operation,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        # Find the operation
        query = select(ConnectorOperationModel).where(
            and_(
                ConnectorOperationModel.id == UUID(op_id),
                ConnectorOperationModel.connector_id == UUID(connector_id),
            )
        )
        result = await session.execute(query)
        op = result.scalar_one_or_none()

        if op is None:
            raise HTTPException(status_code=404, detail="Operation not found")

        if op.source == "custom" and op.type_operation_id is not None:
            # This is an existing override -- toggle its is_enabled_override
            if op.is_enabled_override is False:  # type: ignore[comparison-overlap]  # SQLAlchemy ORM Column vs literal comparison
                # Currently disabled -- re-enable by deleting the override
                # (reverts to type-level which is enabled by default)
                await reset_operation(
                    session=session,
                    connector_id=UUID(connector_id),
                    operation_id=UUID(op_id),
                )
                # Return the type-level op instead
                type_query = select(ConnectorOperationModel).where(
                    ConnectorOperationModel.id == op.type_operation_id
                )
                type_result = await session.execute(type_query)
                type_op = type_result.scalar_one_or_none()
                if type_op:
                    await session.commit()
                    return _model_to_response(type_op)
            else:
                # Currently enabled -- disable
                op.is_enabled_override = False  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
                await session.flush()
                await session.commit()
                return _model_to_response(op)
        elif op.source == "custom":
            # Purely custom op -- toggle is_enabled directly
            op.is_enabled = not op.is_enabled  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
            await session.flush()
            await session.commit()
            return _model_to_response(op)
        else:
            # Type-level op -- create a disable override
            try:
                override = await disable_operation(
                    session=session,
                    connector_id=UUID(connector_id),
                    type_operation_id=UUID(op_id),
                )
                await session.commit()
                return _model_to_response(override)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e


@router.post(
    "/{connector_id}/operations/sync",
    response_model=SyncOperationsResponse,
    responses={
        400: {"description": "Operation sync only supported for VMware connectors, got:..."},
        404: {"description": "Connector not found"},
    },
)
async def sync_connector_operations(
    connector_id: str, user: Annotated[UserContext, Depends(get_current_user)]
):
    """
    Sync operations for a VMware connector with the latest definitions.

    Use this endpoint after MEHO updates to ensure existing connectors
    have access to newly added operations (like detailed performance metrics).

    This will:
    1. Add any new operations that don't exist
    2. Update descriptions/parameters of existing operations
    3. NOT delete any custom operations you may have added
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import (
        ConnectorOperationRepository,
        ConnectorRepository,
    )
    from meho_app.modules.connectors.schemas import ConnectorOperationCreate
    from meho_app.modules.connectors.vmware import VMWARE_OPERATIONS

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        if connector.connector_type != "vmware":
            raise HTTPException(
                status_code=400,
                detail=f"Operation sync only supported for VMware connectors, got: {connector.connector_type}",
            )

        op_repo = ConnectorOperationRepository(session)

        existing_ops = await op_repo.list_operations(connector_id=connector_id, limit=1000)
        existing_op_ids = {op.operation_id for op in existing_ops}

        added = 0
        updated = 0

        for op in VMWARE_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"

            if op.operation_id not in existing_op_ids:
                await op_repo.create_operation(
                    ConnectorOperationCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        operation_id=op.operation_id,
                        name=op.name,
                        description=op.description,
                        category=op.category,
                        parameters=list(op.parameters),
                        example=op.example,
                        search_content=search_content,
                        # TASK-161: Response schema fields for token-aware caching
                        response_entity_type=op.response_entity_type,
                        response_identifier_field=op.response_identifier_field,
                        response_display_name_field=op.response_display_name_field,
                    )
                )
                added += 1
            else:
                await op_repo.update_operation(
                    connector_id=connector_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=list(op.parameters),
                    example=op.example,
                    search_content=search_content,
                    # TASK-161: Response schema fields for token-aware caching
                    response_entity_type=op.response_entity_type,
                    response_identifier_field=op.response_identifier_field,
                    response_display_name_field=op.response_display_name_field,
                )
                updated += 1

        await session.commit()

        return SyncOperationsResponse(
            connector_id=connector_id,
            operations_added=added,
            operations_updated=updated,
            operations_total=len(VMWARE_OPERATIONS),
            message=f"Synced {added} new operations, updated {updated} existing operations.",
        )
