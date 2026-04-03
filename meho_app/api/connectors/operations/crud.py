# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connector CRUD operations.

Basic create, read, update, delete operations for connectors.
"""
# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"

import asyncio
import ipaddress
import socket
from datetime import UTC
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    ConnectorResponse,
    CreateConnectorRequest,
    UpdateConnectorRequest,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.credential_masker import mask_credentials
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

MSG_CONNECTOR_NOT_FOUND = "Connector not found"


def validate_openapi_url(url: str) -> str:
    """Validate URL is safe for server-side fetch (no SSRF).

    Rejects non-http(s) schemes and URLs resolving to private/internal addresses
    (RFC1918, loopback, link-local).

    Args:
        url: The URL to validate.

    Returns:
        The validated URL (unchanged).

    Raises:
        HTTPException: 400 if URL is invalid or resolves to a private address.
    """
    parsed = urlparse(url)

    # Scheme must be http or https
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed.",
        )

    # Block RFC1918, link-local, loopback
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="URL has no hostname.")

    try:
        addr_info = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise HTTPException(
                    status_code=400,
                    detail="URL resolves to private/internal address. External URLs only.",
                )
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve hostname: {hostname}") from exc

    return url


router = APIRouter()


# =============================================================================
# Helper Functions for Auto-Ingestion
# =============================================================================


async def _auto_ingest_soap_wsdl(
    connector_id: str,
    tenant_id: str,
    protocol_config: dict,
    session,
) -> None:
    """Auto-ingest WSDL for SOAP connectors."""
    from meho_app.modules.connectors.soap import (
        SOAPConnectorConfig,
        SoapOperationRepository,
        SOAPSchemaIngester,
        SoapTypeRepository,
    )
    from meho_app.modules.connectors.soap.schemas import (
        SoapOperationDescriptorCreate,
        SoapTypeDescriptorCreate,
    )

    wsdl_url = protocol_config["wsdl_url"]
    logger.info(f"🔄 Auto-ingesting WSDL for SOAP connector: {wsdl_url}")

    soap_config = SOAPConnectorConfig(
        wsdl_url=wsdl_url,
        verify_ssl=protocol_config.get("verify_ssl", True),
        timeout=protocol_config.get("timeout", 30),
    )

    ingester = SOAPSchemaIngester(config=soap_config)
    operations, _metadata, type_definitions = await asyncio.to_thread(
        ingester.ingest_wsdl,
        wsdl_url=wsdl_url,
        connector_id=connector_id,
        tenant_id=tenant_id,
    )

    logger.info(
        f"✅ Discovered {len(operations)} SOAP operations and {len(type_definitions)} types from WSDL"
    )

    soap_op_repo = SoapOperationRepository(session)
    soap_type_repo = SoapTypeRepository(session)

    # Convert SOAPOperation to SoapOperationDescriptorCreate
    op_creates = []
    for op in operations:
        search_content = f"{op.name} {op.operation_name} {op.service_name} {op.description or ''}"
        op_creates.append(
            SoapOperationDescriptorCreate(
                connector_id=str(connector_id),
                tenant_id=tenant_id,
                service_name=op.service_name,
                port_name=op.port_name,
                operation_name=op.operation_name,
                name=op.name,
                description=op.description,
                soap_action=op.soap_action,
                namespace=op.namespace,
                style=op.style.value if hasattr(op.style, "value") else str(op.style),
                input_schema=op.input_schema or {},
                output_schema=op.output_schema or {},
                protocol_details=op.protocol_details or {},
                search_content=search_content,
            )
        )

    # Convert SOAPTypeDefinition to SoapTypeDescriptorCreate
    type_creates = []
    for type_def in type_definitions:
        prop_names = " ".join(p.name for p in type_def.properties)
        search_content = f"{type_def.name} {type_def.base_type or ''} {prop_names}"
        type_creates.append(
            SoapTypeDescriptorCreate(
                connector_id=str(connector_id),
                tenant_id=tenant_id,
                type_name=type_def.name,
                namespace=type_def.namespace,
                base_type=type_def.base_type,
                properties=[p.model_dump() for p in type_def.properties],  # type: ignore[misc]  # dict satisfies SoapPropertySchema at runtime
                description=type_def.description,
                search_content=search_content,
            )
        )

    # Bulk create operations and types
    ops_count = await soap_op_repo.create_operations_bulk(op_creates)
    types_count = await soap_type_repo.create_types_bulk(type_creates)
    await session.commit()

    logger.info(f"✅ Stored {ops_count} SOAP operations and {types_count} types in database")


async def _auto_ingest_openapi_spec(
    connector_id: str,
    tenant_id: str,
    protocol_config: dict,
    session,
) -> None:
    """Auto-fetch and ingest OpenAPI spec for REST connectors."""
    from datetime import datetime

    from meho_app.modules.connectors.repositories import ConnectorTypeRepository
    from meho_app.modules.connectors.rest.repository import (
        EndpointDescriptorRepository,
        OpenAPISpecRepository,
    )
    from meho_app.modules.connectors.rest.schemas import EndpointDescriptorCreate
    from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser
    from meho_app.modules.connectors.schemas import ConnectorEntityTypeCreate
    from meho_app.modules.knowledge.object_storage import ObjectStorage

    openapi_url = protocol_config["openapi_url"]
    logger.info(f"🔄 Auto-fetching OpenAPI spec for REST connector: {openapi_url}")

    # SECURITY: Validate URL before server-side fetch (SSRF prevention)
    validate_openapi_url(openapi_url)

    # Fetch spec from URL
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:  # noqa: S501 -- internal service, self-signed cert
        response = await client.get(openapi_url)
        response.raise_for_status()
        spec_content = response.content

    logger.info(f"✅ Fetched OpenAPI spec ({len(spec_content)} bytes) from {openapi_url}")

    # Parse and validate the spec
    parser = OpenAPIParser()
    spec_dict = parser.parse(spec_content.decode("utf-8"))
    parser.validate_spec(spec_dict)

    # Get version info
    openapi_version = spec_dict.get("openapi", "unknown")
    api_title = spec_dict.get("info", {}).get("title", "Unknown API")
    api_version = spec_dict.get("info", {}).get("version", "unknown")

    logger.info(
        f"✅ OpenAPI spec validated: {api_title} v{api_version} (OpenAPI {openapi_version})"
    )

    # Store spec in object storage
    try:
        object_storage = ObjectStorage()
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        file_ext = "yaml" if openapi_url.endswith((".yaml", ".yml")) else "json"
        storage_key = f"connectors/{connector_id}/openapi-spec-{timestamp}.{file_ext}"
        content_type = "application/x-yaml" if file_ext in ["yaml", "yml"] else "application/json"

        storage_uri = object_storage.upload_document(
            file_bytes=spec_content, key=storage_key, content_type=content_type
        )
        logger.info(f"✅ Stored OpenAPI spec in object storage: {storage_uri}")

        # Save spec metadata to database
        spec_repo = OpenAPISpecRepository(session)
        await spec_repo.create_spec(
            connector_id=str(connector_id),
            storage_uri=storage_uri,
            version=openapi_version,
            spec_version=api_version,
        )
    except Exception as e:
        logger.warning(f"⚠️ Failed to store OpenAPI spec in object storage: {e}")

    # Extract and save endpoints
    endpoints = parser.extract_endpoints(spec_dict)
    endpoint_repo = EndpointDescriptorRepository(session)

    for endpoint_data in endpoints:
        endpoint_create = EndpointDescriptorCreate(
            connector_id=str(connector_id),
            method=endpoint_data["method"],
            path=endpoint_data["path"],
            operation_id=endpoint_data.get("operation_id"),
            summary=endpoint_data.get("summary", ""),
            description=endpoint_data.get("description", ""),
            tags=endpoint_data.get("tags", []),
            path_params_schema=endpoint_data.get("path_params_schema", {}),
            query_params_schema=endpoint_data.get("query_params_schema", {}),
            body_schema=endpoint_data.get("body_schema", {}),
            response_schema=endpoint_data.get("response_schema", {}),
            parameter_metadata=endpoint_data.get("parameter_metadata"),
        )
        await endpoint_repo.upsert_endpoint(endpoint_create)

    await session.commit()
    logger.info(f"✅ Stored {len(endpoints)} endpoints from OpenAPI spec")

    # Extract schema types for search_types
    try:
        schema_types = parser.extract_schema_types(spec_dict)
        if schema_types:
            type_repo = ConnectorTypeRepository(session)
            type_creates = []
            for schema_type in schema_types:
                type_creates.append(
                    ConnectorEntityTypeCreate(
                        connector_id=str(connector_id),
                        tenant_id=tenant_id,
                        type_name=schema_type["type_name"],
                        description=schema_type["description"],
                        category=schema_type["category"],
                        properties=schema_type["properties"],
                        search_content=schema_type["search_content"],
                    )
                )
            schema_types_created = await type_repo.create_types_bulk(type_creates)
            await session.commit()
            logger.info(f"✅ Extracted {schema_types_created} schema types from OpenAPI spec")
    except Exception as e:
        logger.warning(f"⚠️ Failed to extract schema types: {e}")


# =============================================================================
# Route Handlers
# =============================================================================


@router.post(
    "/", response_model=ConnectorResponse, responses={500: {"description": "Internal server error"}}
)
async def create_connector(
    request: CreateConnectorRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_CREATE))],
):
    """
    Create a new API connector.

    Connectors define external systems that MEHO can interact with.
    After creating, upload an OpenAPI spec to define available endpoints.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.schemas import ConnectorCreate
    from meho_app.modules.connectors.service import ConnectorService

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            service = ConnectorService(session)

            connector_create = ConnectorCreate(
                name=request.name,
                base_url=request.base_url,
                auth_type=request.auth_type,
                tenant_id=user.tenant_id,
                description=request.description,
                connector_type=request.connector_type,
                protocol_config=request.protocol_config,
                allowed_methods=request.allowed_methods,
                blocked_methods=request.blocked_methods,
                default_safety_level=request.default_safety_level,
                login_url=request.login_url,
                login_method=request.login_method,
                login_config=request.login_config,
            )

            connector = await service.create_connector(connector_create)
            await session.commit()

            # Auto-ingest WSDL for SOAP connectors
            if (
                request.connector_type == "soap"
                and request.protocol_config
                and request.protocol_config.get("wsdl_url")
            ):
                try:
                    await _auto_ingest_soap_wsdl(
                        connector_id=connector.id,
                        tenant_id=user.tenant_id,
                        protocol_config=request.protocol_config,
                        session=session,
                    )
                except Exception as e:
                    logger.warning(
                        f"⚠️ Failed to auto-ingest WSDL "
                        f"(connector created, WSDL can be ingested manually): {e}"
                    )

            # Auto-fetch OpenAPI spec for REST connectors if openapi_url is provided
            if (
                request.connector_type == "rest"
                and request.protocol_config
                and request.protocol_config.get("openapi_url")
            ):
                try:
                    await _auto_ingest_openapi_spec(
                        connector_id=connector.id,
                        tenant_id=user.tenant_id,
                        protocol_config=request.protocol_config,
                        session=session,
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        f"⚠️ Failed to fetch OpenAPI spec from "
                        f"{request.protocol_config['openapi_url']}: HTTP {e.response.status_code}"
                    )
                except Exception as e:
                    logger.warning(
                        f"⚠️ Failed to auto-ingest OpenAPI spec "
                        f"(connector created, spec can be uploaded manually): {e}"
                    )

            # Audit: log connector creation
            try:
                from meho_app.modules.audit.service import AuditService

                audit = AuditService(session)
                await audit.log_event(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    user_email=getattr(user, "email", None),
                    event_type="connector.create",
                    action="create",
                    resource_type="connector",
                    resource_id=str(connector.id),
                    resource_name=connector.name,
                    details={"connector_type": request.connector_type or "rest"},
                    result="success",
                )
                await session.commit()
            except Exception as audit_err:
                logger.warning(f"Audit logging failed for connector create: {audit_err}")

            return ConnectorResponse(**connector.model_dump())
    except Exception as e:
        logger.error(f"Create connector failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/", responses={500: {"description": "Internal server error"}})
async def list_connectors(user: Annotated[UserContext, Depends(get_current_user)]) -> list[dict]:
    """
    List connectors for current tenant.

    Returns all connectors the user has access to.
    Credentials are masked when superadmin views tenant data.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            repo = ConnectorRepository(session)
            connectors = await repo.list_connectors(tenant_id=user.tenant_id, active_only=True)
            # Mask credentials if superadmin is viewing tenant data (Phase 3 - TASK-140)
            return [mask_credentials(connector.model_dump(), user) for connector in connectors]
    except Exception as e:
        logger.error(f"List connectors failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/{connector_id}",
    response_model=ConnectorResponse,
    responses={
        404: {"description": "Connector not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_connector(connector_id: str, user: Annotated[UserContext, Depends(get_current_user)]):
    """
    Get connector details.

    Returns connector configuration and available endpoints.
    Credentials are masked when superadmin views tenant data.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            repo = ConnectorRepository(session)
            connector = await repo.get_connector(connector_id, tenant_id=user.tenant_id)

            if not connector:
                raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

            # Mask credentials if superadmin is viewing tenant data (Phase 3 - TASK-140)
            connector_data = mask_credentials(connector.model_dump(), user)
            return ConnectorResponse(**connector_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get connector failed: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch(
    "/{connector_id}",
    response_model=ConnectorResponse,
    responses={
        400: {"description": "Invalid update data: ..."},
        404: {"description": "Connector not found"},
        500: {"description": "Internal error: ..."},
    },
)
async def update_connector(
    connector_id: str,
    request: UpdateConnectorRequest,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_UPDATE))],
):
    """
    Update connector configuration and safety policies.

    Allows updating:
    - Basic info (name, description, base_url)
    - Safety policies (allowed/blocked methods)
    - Default safety level for new endpoints
    - SESSION auth configuration (login_url, login_method, login_config)
    - Related connectors for topology correlation
    """
    import traceback

    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.schemas import ConnectorUpdate
    from meho_app.modules.connectors.service import ConnectorService

    # Safe (tainted-sql-string): this is a log message f-string, not SQL (false positive from string interpolation detection)
    logger.info(f"Updating connector {connector_id} for tenant {user.tenant_id}")
    logger.info(f"Update request: {request.model_dump(exclude_unset=True)}")

    session_maker = create_openapi_session_maker()

    try:
        async with session_maker() as session:
            service = ConnectorService(session)

            update_data = request.model_dump(exclude_unset=True)
            logger.info(f"Creating ConnectorUpdate with data: {update_data}")

            try:
                update = ConnectorUpdate(**update_data)
                logger.info("ConnectorUpdate created successfully")
            except Exception as e:
                logger.error(f"Failed to create ConnectorUpdate: {e}")
                raise HTTPException(status_code=400, detail=f"Invalid update data: {e!s}") from e

            connector = await service.update_connector(
                connector_id, update, tenant_id=user.tenant_id
            )

            if not connector:
                logger.error(f"Connector {connector_id} not found for tenant {user.tenant_id}")
                raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

            await session.commit()

            # Audit: log connector update
            try:
                from meho_app.modules.audit.service import AuditService

                audit = AuditService(session)
                await audit.log_event(
                    tenant_id=user.tenant_id,
                    user_id=user.user_id,
                    user_email=getattr(user, "email", None),
                    event_type="connector.update",
                    action="update",
                    resource_type="connector",
                    resource_id=connector_id,
                    resource_name=connector.name,
                    details={"fields_updated": list(update_data.keys())},
                    result="success",
                )
                await session.commit()
            except Exception as audit_err:
                logger.warning(f"Audit logging failed for connector update: {audit_err}")

            logger.info(f"Connector {connector_id} updated successfully")
            logger.info(f"Connector data: {connector.model_dump()}")
            return ConnectorResponse(**connector.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating connector {connector_id}: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Internal error: {e!s}") from e


@router.delete("/{connector_id}", responses={404: {"description": "Connector not found"}})
async def delete_connector(
    connector_id: str,
    user: Annotated[UserContext, Depends(RequirePermission(Permission.CONNECTOR_DELETE))],
):
    """
    Delete a connector and all related data.

    This will delete:
    - Connector configuration (database cascade deletes specs, endpoints, credentials)
    - Knowledge chunks from OpenAPI ingestion

    Returns 204 No Content on success, 404 if not found.
    """
    from sqlalchemy import or_, select

    from meho_app.api.database import create_knowledge_session_maker, create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.knowledge.models import KnowledgeChunkModel
    from meho_app.modules.knowledge.repository import KnowledgeRepository

    openapi_session_maker = create_openapi_session_maker()

    async with openapi_session_maker() as session:
        repo = ConnectorRepository(session)

        # Check if connector exists and belongs to tenant
        connector = await repo.get_connector(connector_id, tenant_id=user.tenant_id)
        if not connector:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        connector_name = connector.name

        # Step 1: Delete knowledge chunks created from OpenAPI or SOAP ingestion
        knowledge_deleted = 0
        try:
            knowledge_session_maker = create_knowledge_session_maker()
            async with knowledge_session_maker() as knowledge_session:
                knowledge_repo = KnowledgeRepository(knowledge_session)

                openapi_prefix = f"openapi://{connector_id}/"
                soap_prefix = f"soap://{connector_id}/"

                result = await knowledge_session.execute(
                    select(KnowledgeChunkModel).where(
                        KnowledgeChunkModel.tenant_id == user.tenant_id,
                        or_(
                            KnowledgeChunkModel.source_uri.like(f"{openapi_prefix}%"),
                            KnowledgeChunkModel.source_uri.like(f"{soap_prefix}%"),
                        ),
                    )
                )
                connector_chunks = result.scalars().all()

                for chunk in connector_chunks:
                    deleted = await knowledge_repo.delete_chunk(str(chunk.id))
                    if deleted:
                        knowledge_deleted += 1

                await knowledge_session.commit()

                if knowledge_deleted > 0:
                    logger.info(
                        f"🗑️ Deleted {knowledge_deleted} knowledge chunks for connector {connector_name}"
                    )
        except Exception as e:
            logger.error(
                f"⚠️ Failed to delete knowledge chunks for connector {connector_name}: {e}",
                exc_info=True,
            )

        # Step 2: Delete blob storage files (OpenAPI specs)
        try:
            from meho_app.modules.connectors.rest.repository import OpenAPISpecRepository
            from meho_app.modules.knowledge.object_storage import ObjectStorage

            object_storage = ObjectStorage()
            spec_repo = OpenAPISpecRepository(session)

            openapi_spec = await spec_repo.get_spec_by_connector(connector_id)

            if openapi_spec and openapi_spec.storage_uri:
                storage_key = openapi_spec.storage_uri.split("/", 3)[-1]

                try:
                    object_storage.delete_document(storage_key)
                    logger.info(f"🗑️ Deleted OpenAPI spec file from storage: {storage_key}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete storage file {storage_key}: {e}")
        except Exception as e:
            logger.error(
                f"⚠️ Failed to delete blob storage files for connector {connector_name}: {e}",
                exc_info=True,
            )

        # Step 3: Delete connector (cascade deletes specs, endpoints, credentials)
        deleted = await repo.delete_connector(connector_id, tenant_id=user.tenant_id)

        if not deleted:
            raise HTTPException(status_code=404, detail=MSG_CONNECTOR_NOT_FOUND)

        await session.commit()

        # Audit: log connector deletion
        try:
            from meho_app.modules.audit.service import AuditService

            audit = AuditService(session)
            await audit.log_event(
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                user_email=getattr(user, "email", None),
                event_type="connector.delete",
                action="delete",
                resource_type="connector",
                resource_id=connector_id,
                resource_name=connector_name,
                details={"knowledge_chunks_deleted": knowledge_deleted},
                result="success",
            )
            await session.commit()
        except Exception as audit_err:
            logger.warning(f"Audit logging failed for connector delete: {audit_err}")

        logger.info(
            f"🗑️ Connector deleted: {connector_name} (id={connector_id}) by user {user.user_id}. "
            f"Knowledge chunks deleted: {knowledge_deleted}"
        )

        return {
            "message": "Connector deleted successfully",
            "knowledge_chunks_deleted": knowledge_deleted,
        }
