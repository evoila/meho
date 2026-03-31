# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Endpoint management operations.

Handles listing, updating, and testing connector endpoints.
"""
# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"

from fastapi import APIRouter, Depends, HTTPException, Query

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    EndpointResponse,
    TestEndpointRequest,
    TestEndpointResponse,
    UpdateEndpointRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.get("/{connector_id}/endpoints", response_model=list[EndpointResponse])
async def list_endpoints(
    connector_id: str,
    method: str | None = Query(None),
    is_enabled: bool | None = Query(None),
    safety_level: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags"),
    search: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_READ)),
):
    """
    List endpoints for a connector with filters.

    Supports filtering by:
    - HTTP method (GET, POST, etc.)
    - Enabled status
    - Safety level
    - Tags
    - Search text (in description/path)
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
    from meho_app.modules.connectors.rest.schemas import EndpointFilter

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        repo = EndpointDescriptorRepository(session)

        tag_list = tags.split(",") if tags else None

        filter_obj = EndpointFilter(
            connector_id=connector_id,
            method=method,
            is_enabled=is_enabled,
            safety_level=safety_level,
            tags=tag_list,
            search_text=search,
            limit=limit,
        )

        endpoints = await repo.list_endpoints(filter_obj)

        return [EndpointResponse(**ep.model_dump()) for ep in endpoints]


@router.patch("/{connector_id}/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    connector_id: str,
    endpoint_id: str,
    request: UpdateEndpointRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_UPDATE)),
):
    """
    Update endpoint configuration.

    Allows editing:
    - Enable/disable endpoint
    - Safety level (safe/caution/dangerous)
    - Approval requirement
    - Custom description (enhanced docs)
    - Admin notes (internal)
    - Usage examples
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
    from meho_app.modules.connectors.rest.schemas import EndpointUpdate

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        repo = EndpointDescriptorRepository(session)

        update = EndpointUpdate(**request.model_dump(exclude_unset=True))
        endpoint = await repo.update_endpoint(endpoint_id, update, modified_by=user.user_id)

        if not endpoint:
            raise HTTPException(status_code=404, detail="Endpoint not found")

        return EndpointResponse(**endpoint.model_dump())


@router.post("/{connector_id}/endpoints/{endpoint_id}/test", response_model=TestEndpointResponse)
async def test_endpoint(
    connector_id: str,
    endpoint_id: str,
    request: TestEndpointRequest,
    user: UserContext = Depends(get_current_user),  # noqa: PT028 -- intentional default value
):
    """
    Test an endpoint with live API call.

    Makes a real HTTP request to the endpoint with provided parameters.
    Useful for verifying connectivity and testing before agent use.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.rest.endpoint_testing import OpenAPIService

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        service = OpenAPIService(session)
        result = await service.test_endpoint(
            user_context=user,
            connector_id=connector_id,
            endpoint_id=endpoint_id,
            path_params=request.path_params,
            query_params=request.query_params,
            body=request.body,
        )

        if result.success:
            return TestEndpointResponse(
                status_code=result.status_code or 200,
                headers={},
                body=result.data,
                duration_ms=int(result.duration_ms or 0),
            )
        else:
            return TestEndpointResponse(
                status_code=result.status_code or 500,
                headers={},
                body=None,
                duration_ms=int(result.duration_ms or 0),
                error=result.error,
            )
