# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Schema type operations.

Handles listing and retrieving connector schema types.
"""
# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"

from fastapi import APIRouter, Depends, HTTPException, Query

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import SchemaTypeResponse
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger

logger = get_logger(__name__)

router = APIRouter()


@router.get("/{connector_id}/types", response_model=list[SchemaTypeResponse])
async def list_connector_types(
    connector_id: str,
    search: str | None = Query(None, description="Search in type names/descriptions"),
    category: str | None = Query(
        None, description="Filter by category (model, request, response, error)"
    ),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user),
):
    """
    List schema types for a connector.

    Returns type definitions extracted from OpenAPI components/schemas.
    Works for REST connectors. For SOAP connectors, use /soap-types instead.

    Supports:
    - Text search in type names/descriptions
    - Category filtering (model, request, response, error, collection)
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import (
        ConnectorRepository,
        ConnectorTypeRepository,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        type_repo = ConnectorTypeRepository(session)

        if search:
            types = await type_repo.search_types(connector_id, search, limit)
        else:
            types = await type_repo.list_types(
                connector_id=connector_id, category=category, limit=limit
            )

        return [
            SchemaTypeResponse(
                type_name=t.type_name,
                description=t.description,
                category=t.category,
                properties=t.properties or [],
            )
            for t in types
        ]


@router.get("/{connector_id}/types/{type_name}", response_model=SchemaTypeResponse)
async def get_connector_type(
    connector_id: str, type_name: str, user: UserContext = Depends(get_current_user)
):
    """
    Get a specific schema type definition.

    Returns detailed type information including all properties.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import (
        ConnectorRepository,
        ConnectorTypeRepository,
    )

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        type_repo = ConnectorTypeRepository(session)
        type_def = await type_repo.get_type_by_name(connector_id, type_name)

        if not type_def:
            raise HTTPException(status_code=404, detail=f"Type '{type_name}' not found")

        return SchemaTypeResponse(
            type_name=type_def.type_name,
            description=type_def.description,
            category=type_def.category,
            properties=type_def.properties or [],
        )
