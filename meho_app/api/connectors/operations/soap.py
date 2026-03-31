# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SOAP/WSDL operations.

Handles WSDL ingestion, SOAP operation listing, and SOAP call execution.
"""
# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"

from fastapi import APIRouter, Depends, HTTPException, Query

from meho_app.api.auth import get_current_user
from meho_app.api.connectors.schemas import (
    CallSOAPRequest,
    CallSOAPResponse,
    IngestWSDLRequest,
    IngestWSDLResponse,
    SOAPOperationResponse,
    SOAPTypeResponse,
)
from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.core.permissions import Permission, RequirePermission

logger = get_logger(__name__)

router = APIRouter()


@router.post("/{connector_id}/wsdl", response_model=IngestWSDLResponse)
async def ingest_wsdl(
    connector_id: str,
    request: IngestWSDLRequest,
    user: UserContext = Depends(RequirePermission(Permission.CONNECTOR_CREATE)),
):
    """
    Ingest a WSDL file to discover SOAP operations.

    This:
    1. Parses the WSDL file using zeep
    2. Extracts all SOAP operations with their schemas
    3. Stores operations for search and execution
    4. Ingests operations into knowledge base for natural language search

    Requirements:
    - Connector must have connector_type='soap'
    - WSDL URL must be accessible (or file path on server)
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.schemas import ConnectorUpdate
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

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if getattr(connector, "connector_type", "rest") != "soap":
            raise HTTPException(
                status_code=400,
                detail=f"Connector type is '{getattr(connector, 'connector_type', 'rest')}', expected 'soap'. "
                "Create a SOAP connector to use WSDL ingestion.",
            )

        try:
            protocol_config = getattr(connector, "protocol_config", {}) or {}
            verify_ssl = protocol_config.get("verify_ssl", True)
            timeout = protocol_config.get("timeout", 30)

            soap_config = SOAPConnectorConfig(
                wsdl_url=request.wsdl_url,
                verify_ssl=verify_ssl,
                timeout=timeout,
            )
            ingester = SOAPSchemaIngester(config=soap_config)

            auth = None
            if connector.credential_strategy == "USER_PROVIDED":
                cred_repo = UserCredentialRepository(session)
                user_creds = await cred_repo.get_credentials(user.user_id, connector_id)
                if user_creds:
                    auth = user_creds

            operations, metadata, type_definitions = await ingester.ingest_wsdl(
                wsdl_url=request.wsdl_url,
                connector_id=connector.id,
                tenant_id=user.tenant_id,
                auth=auth,
            )

            logger.info(
                f"✅ Ingested WSDL: {len(operations)} operations, {len(type_definitions)} types from "
                f"{len(metadata.services)} service(s)"
            )

            protocol_config["wsdl_url"] = request.wsdl_url
            protocol_config["services"] = metadata.services
            protocol_config["ports"] = metadata.ports
            protocol_config["operation_count"] = len(operations)

            await connector_repo.update_connector(
                connector_id,
                ConnectorUpdate(protocol_config=protocol_config),
                tenant_id=user.tenant_id,
            )

            soap_op_repo = SoapOperationRepository(session)
            soap_type_repo = SoapTypeRepository(session)

            await soap_op_repo.delete_by_connector(connector_id)
            await soap_type_repo.delete_by_connector(connector_id)

            op_creates = []
            for op in operations:
                search_content = (
                    f"{op.name} {op.operation_name} {op.service_name} {op.description or ''}"
                )
                op_creates.append(
                    SoapOperationDescriptorCreate(
                        connector_id=str(connector.id),
                        tenant_id=user.tenant_id,
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

            type_creates = []
            for type_def in type_definitions:
                prop_names = " ".join(p.name for p in type_def.properties)
                search_content = f"{type_def.name} {type_def.base_type or ''} {prop_names}"
                type_creates.append(
                    SoapTypeDescriptorCreate(
                        connector_id=str(connector.id),
                        tenant_id=user.tenant_id,
                        type_name=type_def.name,
                        namespace=type_def.namespace,
                        base_type=type_def.base_type,
                        properties=[p.model_dump() for p in type_def.properties],  # type: ignore[misc]
                        description=type_def.description,
                        search_content=search_content,
                    )
                )

            ops_count = await soap_op_repo.create_operations_bulk(op_creates)
            types_count = await soap_type_repo.create_types_bulk(type_creates)

            await session.commit()

            logger.info(
                f"✅ Stored {ops_count} SOAP operations and {types_count} types in database"
            )

            return IngestWSDLResponse(
                message=f"Successfully ingested {ops_count} SOAP operations and {types_count} types",
                wsdl_url=request.wsdl_url,
                operations_count=ops_count,
                types_count=types_count,
                services=metadata.services,
                ports=metadata.ports,
            )

        except ValueError as e:
            logger.error(f"❌ WSDL parsing failed: {e}")
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"❌ WSDL ingestion failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"WSDL ingestion failed: {e}") from e


@router.get("/{connector_id}/soap-operations", response_model=list[SOAPOperationResponse])
async def list_soap_operations(
    connector_id: str,
    search: str | None = Query(None, description="Search in operation names/descriptions"),
    service: str | None = Query(None, description="Filter by service name"),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user),
):
    """
    List SOAP operations for a connector.

    Returns operations from the database (ingested from WSDL).
    Supports filtering by service name and text search.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.soap import SoapOperationRepository

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if getattr(connector, "connector_type", "rest") != "soap":
            raise HTTPException(status_code=400, detail="Connector is not a SOAP connector")

        soap_op_repo = SoapOperationRepository(session)

        if search:
            operations = await soap_op_repo.search_operations(connector_id, search, limit)
        else:
            operations = await soap_op_repo.list_operations(
                connector_id=connector_id, service_name=service, limit=limit
            )

        if not operations:
            protocol_config = getattr(connector, "protocol_config", {}) or {}
            if not protocol_config.get("wsdl_url"):
                raise HTTPException(
                    status_code=400,
                    detail="No WSDL has been ingested for this connector. "
                    "Use POST /{connector_id}/wsdl first.",
                )

        return [
            SOAPOperationResponse(
                name=op.name,
                service_name=op.service_name,
                port_name=op.port_name,
                operation_name=op.operation_name,
                description=op.description,
                soap_action=op.soap_action,
                style=op.style,
                namespace=op.namespace,
                input_schema=op.input_schema,
                output_schema=op.output_schema,
                is_enabled=op.is_enabled,
            )
            for op in operations
        ]


@router.get("/{connector_id}/soap-types", response_model=list[SOAPTypeResponse])
async def list_soap_types(
    connector_id: str,
    search: str | None = Query(None, description="Search in type names/descriptions"),
    base_type: str | None = Query(None, description="Filter by base type"),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user),
):
    """
    List SOAP types for a connector.

    Returns type definitions from the database (ingested from WSDL).
    Supports filtering by base type and text search.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.soap import SoapTypeRepository

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if getattr(connector, "connector_type", "rest") != "soap":
            raise HTTPException(status_code=400, detail="Connector is not a SOAP connector")

        soap_type_repo = SoapTypeRepository(session)

        if search:
            types = await soap_type_repo.search_types(connector_id, search, limit)
        else:
            types = await soap_type_repo.list_types(
                connector_id=connector_id, base_type=base_type, limit=limit
            )

        if not types:
            protocol_config = getattr(connector, "protocol_config", {}) or {}
            if not protocol_config.get("wsdl_url"):
                raise HTTPException(
                    status_code=400,
                    detail="No WSDL has been ingested for this connector. "
                    "Use POST /{connector_id}/wsdl first.",
                )

        return [
            SOAPTypeResponse(
                type_name=t.type_name,
                namespace=t.namespace,
                base_type=t.base_type,
                properties=[
                    p.model_dump() if hasattr(p, "model_dump") else p  # type: ignore[misc]
                    for p in (t.properties or [])
                ],
                description=t.description,
            )
            for t in types
        ]


@router.get("/{connector_id}/soap-types/{type_name}", response_model=SOAPTypeResponse)
async def get_soap_type(
    connector_id: str, type_name: str, user: UserContext = Depends(get_current_user)
):
    """
    Get a specific SOAP type definition.

    Returns detailed type information including all properties.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.soap import SoapTypeRepository

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if getattr(connector, "connector_type", "rest") != "soap":
            raise HTTPException(status_code=400, detail="Connector is not a SOAP connector")

        soap_type_repo = SoapTypeRepository(session)
        type_def = await soap_type_repo.get_type_by_name(connector_id, type_name)

        if not type_def:
            raise HTTPException(status_code=404, detail=f"Type '{type_name}' not found")

        return SOAPTypeResponse(
            type_name=type_def.type_name,
            namespace=type_def.namespace,
            base_type=type_def.base_type,
            properties=[
                p.model_dump() if hasattr(p, "model_dump") else p  # type: ignore[misc]
                for p in (type_def.properties or [])
            ],
            description=type_def.description,
        )


@router.post(
    "/{connector_id}/soap-operations/{operation_name}/call", response_model=CallSOAPResponse
)
async def call_soap_operation(
    connector_id: str,
    operation_name: str,
    request: CallSOAPRequest,
    user: UserContext = Depends(get_current_user),
):
    """
    Call a SOAP operation.

    Executes a SOAP operation with the provided parameters.
    For VMware VIM API, this handles session-based authentication automatically.
    """
    from meho_app.api.database import create_openapi_session_maker
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.repositories.credential_repository import (
        UserCredentialRepository,
    )
    from meho_app.modules.connectors.soap import SOAPAuthType, SOAPClient, SOAPConnectorConfig
    from meho_app.modules.connectors.soap.client import VMwareSOAPClient

    session_maker = create_openapi_session_maker()

    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)

        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")

        if getattr(connector, "connector_type", "rest") != "soap":
            raise HTTPException(status_code=400, detail="Connector is not a SOAP connector")

        protocol_config = getattr(connector, "protocol_config", {}) or {}
        wsdl_url = protocol_config.get("wsdl_url")

        if not wsdl_url:
            raise HTTPException(
                status_code=400, detail="No WSDL has been ingested for this connector"
            )

        credentials = None
        if connector.credential_strategy == "USER_PROVIDED":
            cred_repo = UserCredentialRepository(session)
            credentials = await cred_repo.get_credentials(user.user_id, connector_id)

            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Credentials required. Please configure credentials first.",
                )

        auth_type = SOAPAuthType.NONE
        if connector.auth_type == "BASIC":
            auth_type = SOAPAuthType.BASIC
        elif connector.auth_type == "SESSION":
            auth_type = SOAPAuthType.SESSION

        config = SOAPConnectorConfig(
            wsdl_url=wsdl_url,
            auth_type=auth_type,
            username=credentials.get("username") if credentials else None,
            password=credentials.get("password") if credentials else None,
            login_operation=protocol_config.get("login_operation"),
            logout_operation=protocol_config.get("logout_operation"),
            verify_ssl=protocol_config.get("verify_ssl", False),
        )

        is_vmware = (
            "vmware" in connector.name.lower()
            or "vim" in wsdl_url.lower()
            or "vsphere" in connector.name.lower()
        )

        ClientClass = VMwareSOAPClient if is_vmware else SOAPClient

        try:
            async with ClientClass(config) as client:
                response = await client.call(
                    operation_name=operation_name,
                    params=request.params,
                    service_name=request.service_name,
                    port_name=request.port_name,
                )

                return CallSOAPResponse(
                    success=response.success,
                    status_code=response.status_code,
                    body=response.body,
                    fault_code=response.fault_code,
                    fault_string=response.fault_string,
                    duration_ms=response.duration_ms,
                )

        except Exception as e:
            logger.error(f"SOAP call failed: {e}", exc_info=True)
            return CallSOAPResponse(
                success=False,
                status_code=500,
                body={"error": str(e)},
                fault_string=str(e),
            )
