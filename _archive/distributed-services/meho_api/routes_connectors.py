"""
Connector management routes for MEHO API.

Handles connector creation, OpenAPI spec upload, credential management,
and endpoint enhancement (Task 22).
"""
# mypy: disable-error-code="no-untyped-def,arg-type,attr-defined"
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Query, Body
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime
import httpx
import logging
from meho_core.auth_context import UserContext
from meho_api.auth import get_current_user
from meho_api.config import get_api_config

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/connectors", tags=["connectors"])


class CreateConnectorRequest(BaseModel):
    """Request to create a new connector"""
    name: str
    base_url: str
    auth_type: str = "API_KEY"  # API_KEY, BASIC, OAUTH2, NONE, SESSION
    description: Optional[str] = None
    # Connector type - single source of truth for classification
    connector_type: Literal["rest", "soap", "graphql", "grpc", "vmware", "kubernetes"] = "rest"
    protocol_config: Optional[Dict[str, Any]] = None
    # Task 22: Safety policies
    allowed_methods: List[str] = Field(default=["GET", "POST", "PUT", "PATCH", "DELETE"])
    blocked_methods: List[str] = Field(default_factory=list)
    default_safety_level: Literal["safe", "caution", "dangerous"] = "safe"
    # Task 56: SESSION auth fields
    login_url: Optional[str] = None
    login_method: Optional[str] = None
    login_config: Optional[Dict[str, Any]] = None


class ConnectorResponse(BaseModel):
    """Connector response"""
    id: str
    name: str
    base_url: str
    auth_type: str
    description: Optional[str]
    tenant_id: str
    # Connector type - single source of truth for classification
    connector_type: str = "rest"
    protocol_config: Optional[Dict[str, Any]] = None
    allowed_methods: List[str]
    blocked_methods: List[str]
    default_safety_level: str
    is_active: bool
    # SESSION auth fields
    login_url: Optional[str] = None
    login_method: Optional[str] = None
    login_config: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime


class UpdateConnectorRequest(BaseModel):
    """Update connector (Task 22 + Task 29 + Session 54/55)"""
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None  # Task 29: Allow fixing base URL
    # Connector type - single source of truth for classification
    auth_type: Optional[str] = None  # API_KEY, BASIC, OAUTH2, NONE, SESSION
    connector_type: Optional[Literal["rest", "soap", "graphql", "grpc", "vmware", "kubernetes"]] = None
    protocol_config: Optional[Dict[str, Any]] = None
    allowed_methods: Optional[List[str]] = None
    blocked_methods: Optional[List[str]] = None
    default_safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None
    is_active: Optional[bool] = None
    # SESSION auth configuration (Session 54/55)
    login_url: Optional[str] = None
    login_method: Optional[str] = None
    login_config: Optional[Dict[str, Any]] = None


class EndpointResponse(BaseModel):
    """Endpoint response"""
    id: str
    connector_id: str
    method: str
    path: str
    operation_id: Optional[str]
    summary: Optional[str]
    description: Optional[str]
    tags: List[str]
    # Task 22: Enhancement fields
    is_enabled: bool
    safety_level: str
    requires_approval: bool
    custom_description: Optional[str]
    custom_notes: Optional[str]
    usage_examples: Optional[Dict[str, Any]]
    last_modified_by: Optional[str]
    last_modified_at: Optional[datetime]
    created_at: datetime
    # Session 77: Schema fields for data visibility
    path_params_schema: Optional[Dict[str, Any]] = None
    query_params_schema: Optional[Dict[str, Any]] = None
    body_schema: Optional[Dict[str, Any]] = None
    response_schema: Optional[Dict[str, Any]] = None
    required_params: Optional[List[str]] = None
    # Session 78: Explicit parameter metadata for LLM guidance
    parameter_metadata: Optional[Dict[str, Any]] = None


class UpdateEndpointRequest(BaseModel):
    """Update endpoint (Task 22)"""
    is_enabled: Optional[bool] = None
    safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None
    requires_approval: Optional[bool] = None
    custom_description: Optional[str] = None
    custom_notes: Optional[str] = None
    usage_examples: Optional[Dict[str, Any]] = None


class TestEndpointRequest(BaseModel):
    """Test an endpoint"""
    path_params: Dict[str, Any] = Field(default_factory=dict)
    query_params: Dict[str, Any] = Field(default_factory=dict)
    body: Optional[Any] = None
    use_system_credentials: bool = True


class TestEndpointResponse(BaseModel):
    """Test endpoint response"""
    status_code: int
    headers: Dict[str, str]
    body: Any
    duration_ms: int
    error: Optional[str] = None


@router.post("", response_model=ConnectorResponse)
async def create_connector(
    request: CreateConnectorRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Create a new API connector.
    
    Connectors define external systems that MEHO can interact with.
    After creating, upload an OpenAPI spec to define available endpoints.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    from meho_openapi.schemas import ConnectorCreate
    
    session_maker = create_openapi_session_maker()
    
    try:
        async with session_maker() as session:
            repo = ConnectorRepository(session)
            
            connector_create = ConnectorCreate(
                name=request.name,
                base_url=request.base_url,
                auth_type=request.auth_type,
                tenant_id=user.tenant_id,
                description=request.description,
                # Connector type - single source of truth
                connector_type=request.connector_type,
                protocol_config=request.protocol_config,
                allowed_methods=request.allowed_methods,
                blocked_methods=request.blocked_methods,
                default_safety_level=request.default_safety_level,
                # SESSION auth fields
                login_url=request.login_url,
                login_method=request.login_method,
                login_config=request.login_config
            )
            
            connector = await repo.create_connector(connector_create)
            await session.commit()
            
            # Auto-ingest WSDL for SOAP connectors
            if request.connector_type == "soap" and request.protocol_config and request.protocol_config.get("wsdl_url"):
                try:
                    from meho_openapi.soap.ingester import SOAPSchemaIngester
                    from meho_openapi.soap.models import SOAPConnectorConfig
                    wsdl_url = request.protocol_config["wsdl_url"]
                    logger.info(f"🔄 Auto-ingesting WSDL for SOAP connector: {wsdl_url}")
                    
                    soap_config = SOAPConnectorConfig(
                        wsdl_url=wsdl_url,
                        verify_ssl=request.protocol_config.get("verify_ssl", True),
                        timeout=request.protocol_config.get("timeout", 30),
                    )
                    
                    ingester = SOAPSchemaIngester(config=soap_config)
                    # TASK-96: ingest_wsdl returns (operations, metadata, type_definitions)
                    operations, metadata, type_definitions = await ingester.ingest_wsdl(
                        wsdl_url=wsdl_url,
                        connector_id=connector.id,
                        tenant_id=user.tenant_id,
                    )
                    
                    logger.info(f"✅ Discovered {len(operations)} SOAP operations and {len(type_definitions)} types from WSDL")
                    
                    # Store operations and types in DB tables
                    from meho_openapi.repository import SoapOperationRepository, SoapTypeRepository
                    from meho_openapi.schemas import SoapOperationDescriptorCreate, SoapTypeDescriptorCreate
                    
                    soap_op_repo = SoapOperationRepository(session)
                    soap_type_repo = SoapTypeRepository(session)
                    
                    # Convert SOAPOperation to SoapOperationDescriptorCreate
                    op_creates = []
                    for op in operations:
                        search_content = f"{op.name} {op.operation_name} {op.service_name} {op.description or ''}"
                        op_creates.append(SoapOperationDescriptorCreate(
                            connector_id=str(connector.id),
                            tenant_id=user.tenant_id,
                            service_name=op.service_name,
                            port_name=op.port_name,
                            operation_name=op.operation_name,
                            name=op.name,
                            description=op.description,
                            soap_action=op.soap_action,
                            namespace=op.namespace,
                            style=op.style.value if hasattr(op.style, 'value') else str(op.style),
                            input_schema=op.input_schema or {},
                            output_schema=op.output_schema or {},
                            protocol_details=op.protocol_details or {},
                            search_content=search_content,
                        ))
                    
                    # Convert SOAPTypeDefinition to SoapTypeDescriptorCreate
                    type_creates = []
                    for type_def in type_definitions:
                        prop_names = " ".join(p.name for p in type_def.properties)
                        search_content = f"{type_def.name} {type_def.base_type or ''} {prop_names}"
                        type_creates.append(SoapTypeDescriptorCreate(
                            connector_id=str(connector.id),
                            tenant_id=user.tenant_id,
                            type_name=type_def.name,
                            namespace=type_def.namespace,
                            base_type=type_def.base_type,
                            properties=[p.model_dump() for p in type_def.properties],  # type: ignore[misc]
                            description=type_def.description,
                            search_content=search_content,
                        ))
                    
                    # Bulk create operations and types
                    ops_count = await soap_op_repo.create_operations_bulk(op_creates)
                    types_count = await soap_type_repo.create_types_bulk(type_creates)
                    await session.commit()
                    
                    logger.info(f"✅ Stored {ops_count} SOAP operations and {types_count} types in database")
                        
                except Exception as e:
                    # Log but don't fail connector creation
                    logger.warning(f"⚠️ Failed to auto-ingest WSDL (connector created, WSDL can be ingested manually): {e}")
            
            # Auto-fetch OpenAPI spec for REST connectors if openapi_url is provided
            if request.connector_type == "rest" and request.protocol_config and request.protocol_config.get("openapi_url"):
                try:
                    from meho_openapi.repository import EndpointDescriptorRepository, OpenAPISpecRepository, ConnectorTypeRepository
                    from meho_openapi.spec_parser import OpenAPIParser
                    from meho_openapi.schemas import EndpointDescriptorCreate, ConnectorEntityTypeCreate
                    from meho_knowledge.object_storage import ObjectStorage
                    from datetime import datetime
                    
                    openapi_url = request.protocol_config["openapi_url"]
                    logger.info(f"🔄 Auto-fetching OpenAPI spec for REST connector: {openapi_url}")
                    
                    # Fetch spec from URL
                    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
                        response = await client.get(openapi_url)
                        response.raise_for_status()
                        spec_content = response.content
                    
                    logger.info(f"✅ Fetched OpenAPI spec ({len(spec_content)} bytes) from {openapi_url}")
                    
                    # Parse and validate the spec
                    parser = OpenAPIParser()
                    spec_dict = parser.parse(spec_content.decode('utf-8'))
                    parser.validate_spec(spec_dict)
                    
                    # Get version info
                    openapi_version = spec_dict.get('openapi', 'unknown')
                    api_title = spec_dict.get('info', {}).get('title', 'Unknown API')
                    api_version = spec_dict.get('info', {}).get('version', 'unknown')
                    
                    logger.info(f"✅ OpenAPI spec validated: {api_title} v{api_version} (OpenAPI {openapi_version})")
                    
                    # Store spec in object storage
                    try:
                        object_storage = ObjectStorage()
                        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                        # Determine file extension from URL or default to json
                        file_ext = 'yaml' if openapi_url.endswith(('.yaml', '.yml')) else 'json'
                        storage_key = f"connectors/{connector.id}/openapi-spec-{timestamp}.{file_ext}"
                        content_type = 'application/x-yaml' if file_ext in ['yaml', 'yml'] else 'application/json'
                        
                        storage_uri = object_storage.upload_document(
                            file_bytes=spec_content,
                            key=storage_key,
                            content_type=content_type
                        )
                        logger.info(f"✅ Stored OpenAPI spec in object storage: {storage_uri}")
                        
                        # Save spec metadata to database
                        spec_repo = OpenAPISpecRepository(session)
                        await spec_repo.create_spec(
                            connector_id=str(connector.id),
                            storage_uri=storage_uri,
                            version=openapi_version,
                            spec_version=api_version
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to store OpenAPI spec in object storage: {e}")
                    
                    # Extract and save endpoints
                    endpoints = parser.extract_endpoints(spec_dict)
                    endpoint_repo = EndpointDescriptorRepository(session)
                    
                    for endpoint_data in endpoints:
                        endpoint_create = EndpointDescriptorCreate(
                            connector_id=str(connector.id),
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
                            parameter_metadata=endpoint_data.get("parameter_metadata")
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
                                type_creates.append(ConnectorEntityTypeCreate(
                                    connector_id=str(connector.id),
                                    tenant_id=user.tenant_id,
                                    type_name=schema_type["type_name"],
                                    description=schema_type["description"],
                                    category=schema_type["category"],
                                    properties=schema_type["properties"],
                                    search_content=schema_type["search_content"],
                                ))
                            schema_types_created = await type_repo.create_types_bulk(type_creates)
                            await session.commit()
                            logger.info(f"✅ Extracted {schema_types_created} schema types from OpenAPI spec")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to extract schema types: {e}")
                    
                except httpx.HTTPStatusError as e:
                    logger.warning(f"⚠️ Failed to fetch OpenAPI spec from {openapi_url}: HTTP {e.response.status_code}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to auto-ingest OpenAPI spec (connector created, spec can be uploaded manually): {e}")
            
            # Return as ConnectorResponse
            return ConnectorResponse(**connector.model_dump())
    except Exception as e:
        logger.error(f"Create connector failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# VMware Connector (TASK-97)
# ============================================================================

class CreateVMwareConnectorRequest(BaseModel):
    """Request to create a VMware vSphere connector"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    vcenter_host: str = Field(..., description="vCenter Server hostname or IP")
    port: int = Field(default=443, description="vCenter Server port")
    disable_ssl_verification: bool = Field(
        default=False,
        description="Disable SSL certificate verification (not recommended for production)"
    )
    username: str = Field(..., description="vCenter username (e.g., administrator@vsphere.local)")
    password: str = Field(..., description="vCenter password")


class VMwareConnectorResponse(BaseModel):
    """Response after creating a VMware connector"""
    id: str
    name: str
    vcenter_host: str
    connector_type: str = "vmware"
    operations_registered: int
    types_registered: int
    message: str


@router.post("/vmware", response_model=VMwareConnectorResponse)
async def create_vmware_connector(
    request: CreateVMwareConnectorRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Create a VMware vSphere connector (TASK-97).
    
    This creates a typed connector using pyvmomi for native vSphere access.
    Operations are pre-registered based on the VMware connector implementation.
    
    The connector will be tested during creation to verify connectivity.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, ConnectorOperationRepository, ConnectorTypeRepository
    from meho_openapi.schemas import ConnectorCreate, ConnectorOperationCreate, ConnectorEntityTypeCreate
    from meho_openapi.connectors.vmware import (
        VMwareConnector,
        VMWARE_OPERATIONS,
        VMWARE_OPERATIONS_VERSION,
        VMWARE_TYPES,
    )
    
    session_maker = create_openapi_session_maker()
    
    try:
        async with session_maker() as session:
            repo = ConnectorRepository(session)
            op_repo = ConnectorOperationRepository(session)
            type_repo = ConnectorTypeRepository(session)
            
            # Clean up vcenter_host - strip protocol prefix if user included it
            vcenter_host = request.vcenter_host.strip()
            if vcenter_host.startswith("https://"):
                vcenter_host = vcenter_host[8:]  # Remove "https://"
            elif vcenter_host.startswith("http://"):
                vcenter_host = vcenter_host[7:]  # Remove "http://"
            # Also remove trailing slashes
            vcenter_host = vcenter_host.rstrip("/")
            
            # Build protocol config with operations version for auto-sync
            protocol_config = {
                "vcenter_host": vcenter_host,
                "port": request.port,
                "disable_ssl_verification": request.disable_ssl_verification,
                "operations_version": VMWARE_OPERATIONS_VERSION,
            }
            
            # Create connector record
            connector_create = ConnectorCreate(
                name=request.name,
                description=request.description,
                base_url=f"https://{vcenter_host}:{request.port}",
                auth_type="SESSION",
                credential_strategy="USER_PROVIDED",
                tenant_id=user.tenant_id,
                connector_type="vmware",  # VMware typed connector
                protocol_config=protocol_config,
            )
            
            connector = await repo.create_connector(connector_create)
            connector_id = str(connector.id)
            
            # Test connection
            logger.info(f"🔌 Testing vCenter connection: {vcenter_host}")
            try:
                vmware = VMwareConnector(
                    connector_id=connector_id,
                    config=protocol_config,
                    credentials={
                        "username": request.username,
                        "password": request.password,
                    },
                )
                await vmware.connect()
                is_connected = await vmware.test_connection()
                await vmware.disconnect()
                
                if not is_connected:
                    raise HTTPException(
                        status_code=400,
                        detail="Could not connect to vCenter. Check host and credentials."
                    )
                    
                logger.info(f"✅ vCenter connection verified: {vcenter_host}")
            except ImportError:
                logger.warning("⚠️ pyvmomi not installed - skipping connection test")
            except Exception as e:
                logger.warning(f"⚠️ vCenter connection test failed: {e}")
                # Don't fail - allow creation even if we can't test immediately
            
            # Store user credentials
            from meho_openapi.user_credentials import UserCredentialRepository
            from meho_openapi.schemas import UserCredentialProvide
            
            cred_repo = UserCredentialRepository(session)
            await cred_repo.store_credentials(
                user_id=user.user_id,
                credential=UserCredentialProvide(
                    connector_id=connector_id,
                    credential_type="PASSWORD",
                    credentials={
                        "username": request.username,
                        "password": request.password,
                    },
                ),
            )
            
            # Register operations
            op_creates = []
            for op in VMWARE_OPERATIONS:
                search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
                op_creates.append(ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=user.tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=[p for p in op.parameters],
                    example=op.example,
                    search_content=search_content,
                ))
            
            ops_count = await op_repo.create_operations_bulk(op_creates)
            
            # Register entity types
            type_creates = []
            for t in VMWARE_TYPES:
                prop_names = " ".join(p.get("name", "") for p in t.properties)
                search_content = f"{t.type_name} {t.description} {t.category} {prop_names}"
                type_creates.append(ConnectorEntityTypeCreate(
                    connector_id=connector_id,
                    tenant_id=user.tenant_id,
                    type_name=t.type_name,
                    description=t.description,
                    category=t.category,
                    properties=[p for p in t.properties],
                    search_content=search_content,
                ))
            
            types_count = await type_repo.create_types_bulk(type_creates)
            
            await session.commit()
            
            logger.info(f"✅ Created VMware connector '{request.name}' with {ops_count} operations and {types_count} types")
            
            return VMwareConnectorResponse(
                id=connector_id,
                name=request.name,
                vcenter_host=vcenter_host,  # Return cleaned host
                connector_type="vmware",
                operations_registered=ops_count,
                types_registered=types_count,
                message=f"VMware connector created successfully. Registered {ops_count} operations.",
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Create VMware connector failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def list_connectors(
    user: UserContext = Depends(get_current_user)
):
    """
    List connectors for current tenant.
    
    Returns all connectors the user has access to.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    
    session_maker = create_openapi_session_maker()
    
    try:
        async with session_maker() as session:
            repo = ConnectorRepository(session)
            connectors = await repo.list_connectors(tenant_id=user.tenant_id, active_only=True)
            
            # Convert to dict for JSON response
            return [connector.model_dump() for connector in connectors]
    except Exception as e:
        logger.error(f"List connectors failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{connector_id}", response_model=ConnectorResponse)
async def get_connector(
    connector_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Get connector details.
    
    Returns connector configuration and available endpoints.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    
    session_maker = create_openapi_session_maker()
    
    try:
        async with session_maker() as session:
            repo = ConnectorRepository(session)
            connector = await repo.get_connector(connector_id, tenant_id=user.tenant_id)
            
            if not connector:
                raise HTTPException(status_code=404, detail="Connector not found")
            
            # Return as ConnectorResponse (includes SESSION auth fields)
            return ConnectorResponse(**connector.model_dump())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get connector failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{connector_id}/openapi-spec")
async def upload_openapi_spec(
    connector_id: str,
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user)
):
    """
    Upload OpenAPI specification for a connector.
    
    This:
    1. Stores the original file in object storage for debugging/auditing
    2. Parses the spec and creates endpoint descriptors
    3. Ingests endpoints into knowledge base for natural language search
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, EndpointDescriptorRepository, OpenAPISpecRepository
    from meho_openapi.spec_parser import OpenAPIParser
    from meho_openapi.schemas import EndpointDescriptorCreate, EndpointUpdate, EndpointFilter
    from meho_openapi.instruction_generator import InstructionGenerator, should_generate_instructions
    from meho_knowledge.object_storage import ObjectStorage
    import json
    from datetime import datetime
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Read file content
        spec_content = await file.read()
        
        # Step 1: Parse and validate the OpenAPI spec FIRST (before storing)
        parser = OpenAPIParser()
        try:
            spec_dict = parser.parse(spec_content.decode('utf-8'))
            
            # Validate spec structure and version
            parser.validate_spec(spec_dict)
            
            # Get version info for logging
            openapi_version = spec_dict.get('openapi', 'unknown')
            api_title = spec_dict.get('info', {}).get('title', 'Unknown API')
            api_version = spec_dict.get('info', {}).get('version', 'unknown')
            endpoint_count = len(spec_dict.get('paths', {}))
            
            logger.info(
                f"✅ OpenAPI spec validated successfully:\n"
                f"   API: {api_title} v{api_version}\n"
                f"   OpenAPI version: {openapi_version}\n"
                f"   Endpoints: {endpoint_count}"
            )
            
        except ValueError as e:
            # Validation failed - return clear error message
            logger.error(f"❌ OpenAPI spec validation failed: {e}")
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            # Parse failed - return clear error message
            logger.error(f"❌ Failed to parse OpenAPI spec: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to parse OpenAPI spec: {e}")
        
        # Step 2: Store original file in object storage (only if valid!)
        try:
            object_storage = ObjectStorage()
            # Create storage key: connectors/{connector_id}/openapi-spec-{timestamp}.{extension}
            timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            filename = file.filename or 'openapi-spec.json'
            file_ext = filename.split('.')[-1] if '.' in filename else 'json'
            storage_key = f"connectors/{connector_id}/openapi-spec-{timestamp}.{file_ext}"
            
            # Determine content type
            content_type = file.content_type or 'application/json'
            if file_ext in ['yaml', 'yml']:
                content_type = 'application/x-yaml'
            
            storage_uri = object_storage.upload_document(
                file_bytes=spec_content,
                key=storage_key,
                content_type=content_type
            )
            
            logger.info(f"✅ Stored OpenAPI spec in object storage: {storage_uri}")
        except Exception as e:
            logger.error(f"Failed to store OpenAPI spec in object storage: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to store spec file: {e}")
        
        # Step 3: Save OpenAPI spec metadata to database
        spec_repo = OpenAPISpecRepository(session)
        
        openapi_spec = await spec_repo.create_spec(
            connector_id=connector_id,
            storage_uri=storage_uri,
            version=openapi_version,
            spec_version=api_version
        )
        
        logger.info(f"✅ Created OpenAPI spec metadata record (id={openapi_spec.id})")
        
        # Step 4: Extract endpoints from spec (already validated)
        endpoints = parser.extract_endpoints(spec_dict)
        
        # Step 5: Save endpoints to database (upsert to prevent duplicates on re-upload)
        endpoint_repo = EndpointDescriptorRepository(session)
        
        for endpoint_data in endpoints:
            endpoint_create = EndpointDescriptorCreate(
                connector_id=connector_id,
                method=endpoint_data["method"],
                path=endpoint_data["path"],
                operation_id=endpoint_data.get("operation_id"),
                summary=endpoint_data.get("summary", ""),
                description=endpoint_data.get("description", ""),
                tags=endpoint_data.get("tags", []),
                # Include all extracted schemas
                path_params_schema=endpoint_data.get("path_params_schema", {}),
                query_params_schema=endpoint_data.get("query_params_schema", {}),
                body_schema=endpoint_data.get("body_schema", {}),
                response_schema=endpoint_data.get("response_schema", {}),
                # Session 78: Include parameter metadata for LLM guidance
                parameter_metadata=endpoint_data.get("parameter_metadata")
            )
            # Use upsert to update existing endpoints instead of creating duplicates
            await endpoint_repo.upsert_endpoint(endpoint_create)
        
        await session.commit()
        
        # TASK-98: Extract and store OpenAPI schema types for search_types
        # This enables "What is a User?" queries for REST connectors
        schema_types_created = 0
        try:
            from meho_openapi.repository import ConnectorTypeRepository
            from meho_openapi.schemas import ConnectorEntityTypeCreate
            
            # Extract schema types from components/schemas (shallow - keeps type refs)
            schema_types = parser.extract_schema_types(spec_dict)
            
            if schema_types:
                type_repo = ConnectorTypeRepository(session)
                
                # Delete existing types for this connector (re-ingestion case)
                await type_repo.delete_by_connector(connector_id)
                
                # Create schema types
                type_creates = []
                for schema_type in schema_types:
                    type_creates.append(ConnectorEntityTypeCreate(
                        connector_id=connector_id,
                        tenant_id=user.tenant_id,
                        type_name=schema_type["type_name"],
                        description=schema_type["description"],
                        category=schema_type["category"],
                        properties=schema_type["properties"],
                        search_content=schema_type["search_content"],
                    ))
                
                schema_types_created = await type_repo.create_types_bulk(type_creates)
                await session.commit()
                
                logger.info(
                    f"✅ Extracted {schema_types_created} schema types from OpenAPI spec "
                    f"for connector {connector.name}"
                )
        except Exception as e:
            # Log error but don't fail - schema extraction is optional enhancement
            logger.warning(f"Failed to extract OpenAPI schema types: {e}")
        
        # TASK-81: Generate LLM instructions for complex write endpoints
        # These guide the agent in helping users through parameter collection
        instructions_generated = 0
        try:
            instruction_generator = InstructionGenerator(use_llm=False)  # Rule-based for speed
            
            for endpoint_data in endpoints:
                method = endpoint_data["method"]
                body_schema = endpoint_data.get("body_schema", {})
                
                # Only generate for complex write endpoints
                if not should_generate_instructions(method, body_schema):
                    continue
                
                # Generate instructions
                instructions = await instruction_generator.generate_for_endpoint(
                    endpoint_id=endpoint_data.get("operation_id", ""),
                    method=method,
                    path=endpoint_data["path"],
                    body_schema=body_schema,
                    description=endpoint_data.get("description"),
                    summary=endpoint_data.get("summary"),
                    operation_id=endpoint_data.get("operation_id"),
                )
                
                # Update endpoint with instructions
                # Find the endpoint by path + method
                existing_endpoints = await endpoint_repo.list_endpoints(
                    EndpointFilter(
                        connector_id=connector_id,
                        method=method,
                        limit=500
                    )
                )
                
                # Find the matching endpoint
                for existing in existing_endpoints:
                    if existing.path == endpoint_data["path"]:
                        await endpoint_repo.update_endpoint(
                            existing.id,
                            EndpointUpdate(llm_instructions=instructions.model_dump()),
                            modified_by=f"system:instruction_generator"
                        )
                        instructions_generated += 1
                        break
            
            if instructions_generated > 0:
                await session.commit()
                logger.info(
                    f"✅ Generated LLM instructions for {instructions_generated} complex write endpoints"
                )
        except Exception as e:
            # Log error but don't fail - instructions are optional enhancement
            logger.warning(f"Failed to generate LLM instructions: {e}")
        
        # TASK-55: Ingest OpenAPI endpoints into knowledge base for natural language search
        knowledge_chunks_created = 0
        try:
            from meho_openapi.knowledge_ingestion import ingest_openapi_to_knowledge
            from meho_knowledge.knowledge_store import KnowledgeStore
            from meho_knowledge.repository import KnowledgeRepository
            from meho_knowledge.embeddings import get_embedding_provider
            from meho_knowledge.hybrid_search import PostgresFTSHybridService
            from meho_api.database import create_knowledge_session_maker
            
            # Get knowledge store
            knowledge_session_maker = create_knowledge_session_maker()
            async with knowledge_session_maker() as knowledge_session:
                # Create knowledge store components manually (outside FastAPI dependency injection)
                repository = KnowledgeRepository(knowledge_session)
                embedding_provider = get_embedding_provider()
                
                # Create hybrid search service (PostgreSQL FTS + semantic)
                # No BM25 manager needed - uses PostgreSQL built-in full-text search
                hybrid_search = PostgresFTSHybridService(repository, embedding_provider)
                
                # Create knowledge store WITH hybrid search support
                knowledge_store = KnowledgeStore(
                    repository=repository,
                    embedding_provider=embedding_provider,
                    hybrid_search_service=hybrid_search
                )
                
                # Ingest endpoints as searchable knowledge
                knowledge_chunks_created = await ingest_openapi_to_knowledge(
                    spec_dict=spec_dict,
                    connector_id=connector_id,
                    connector_name=connector.name,
                    knowledge_store=knowledge_store,
                    user_context=user
                )
                
                # Commit the knowledge session to make chunks searchable
                await knowledge_session.commit()
                
                logger.info(
                    f"✅ Ingested {knowledge_chunks_created} OpenAPI endpoints to knowledge base "
                    f"for connector {connector.name}"
                )
                
                # NOTE: No manual index rebuilding needed!
                # PostgreSQL FTS indexes are automatically maintained by the database.
                # The GIN index on knowledge_chunk.text is updated on every INSERT/UPDATE.
        except Exception as e:
            # Log error but don't fail the upload - endpoints are still usable
            logger.error(
                f"⚠️  Failed to ingest OpenAPI endpoints to knowledge base: {e}. "
                f"Endpoints are saved but won't be searchable by natural language.",
                exc_info=True
            )
        
        return {
            "message": f"Uploaded OpenAPI spec with {len(endpoints)} endpoints and {schema_types_created} types",
            "api_title": api_title,
            "api_version": api_version,
            "openapi_version": openapi_version,
            "endpoints_count": len(endpoints),
            "schema_types_count": schema_types_created,  # TASK-98: OpenAPI schema types extracted
            "knowledge_chunks_created": knowledge_chunks_created,
            "storage_uri": storage_uri,
            "spec_id": openapi_spec.id
        }


@router.get("/{connector_id}/openapi-spec/download")
async def download_openapi_spec(
    connector_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Download the original OpenAPI specification file.
    
    Returns the stored spec file for debugging, auditing, or re-use.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, OpenAPISpecRepository
    from meho_knowledge.object_storage import ObjectStorage
    from fastapi.responses import Response
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Get OpenAPI spec record
        spec_repo = OpenAPISpecRepository(session)
        openapi_spec = await spec_repo.get_spec_by_connector(connector_id)
        
        if not openapi_spec or not openapi_spec.storage_uri:
            raise HTTPException(status_code=404, detail="No OpenAPI spec found for this connector")
        
        # Extract storage key from s3://bucket/key format
        storage_key = openapi_spec.storage_uri.split('/', 3)[-1]
        
        # Download from object storage
        try:
            object_storage = ObjectStorage()
            file_bytes = object_storage.download_document(storage_key)
            
            # Determine content type and filename
            file_ext = storage_key.split('.')[-1]
            if file_ext in ['yaml', 'yml']:
                content_type = 'application/x-yaml'
                filename = f"{connector.name.replace(' ', '-')}-openapi.yaml"
            else:
                content_type = 'application/json'
                filename = f"{connector.name.replace(' ', '-')}-openapi.json"
            
            logger.info(
                f"📥 User {user.user_id} downloaded OpenAPI spec for connector {connector.name}"
            )
            
            return Response(
                content=file_bytes,
                media_type=content_type,
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"'
                }
            )
        except Exception as e:
            logger.error(f"Failed to download OpenAPI spec: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to download spec: {e}")


@router.post("/{connector_id}/credentials")
async def set_user_credentials(
    connector_id: str,
    credentials: dict,
    user: UserContext = Depends(get_current_user)
):
    """
    Set user-specific credentials for a connector.
    
    For connectors with USER_PROVIDED credential strategy,
    each user provides their own credentials (e.g., vSphere, K8s).
    
    This ensures audit trails show actual users, not "MEHO system".
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.repository import ConnectorRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Store user credentials
        from meho_openapi.user_credentials import UserCredentialProvide
        cred_repo = UserCredentialRepository(session)
        
        # Determine credential type based on auth_type
        if connector.auth_type == "BASIC":
            credential_type = "PASSWORD"
        elif connector.auth_type == "SESSION":
            credential_type = "SESSION"
        else:  # API_KEY, OAUTH2, etc
            credential_type = "API_KEY"
        
        # Create credential provide object
        credential = UserCredentialProvide(
            connector_id=connector_id,
            credential_type=credential_type,
            credentials=credentials
        )
        
        await cred_repo.store_credentials(
            user_id=user.user_id,
            credential=credential
        )
        
        # Commit the credential save
        await session.commit()
        
        return {"status": "success", "message": "Credentials saved"}


@router.get("/{connector_id}/credentials/status")
async def get_credential_status(
    connector_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Check if user has credentials configured for a connector (Task 29).
    
    Returns credential status WITHOUT exposing actual credentials.
    Security: Never returns actual passwords or keys.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.repository import ConnectorRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Check if user has credentials
        cred_repo = UserCredentialRepository(session)
        record = await cred_repo._get_credential_record(user.user_id, connector_id)
        
        if not record or not record.is_active:
            return {
                "has_credentials": False,
                "credential_type": None,
                "last_used_at": None
            }
        
        return {
            "has_credentials": True,
            "credential_type": record.credential_type,
            "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None
        }


@router.delete("/{connector_id}/credentials")
async def delete_user_credentials(
    connector_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Delete user's credentials for a connector (Task 29).
    
    Security: Users can only delete their own credentials.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.repository import ConnectorRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if connector.tenant_id != user.tenant_id:
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Delete credentials
        cred_repo = UserCredentialRepository(session)
        deleted = await cred_repo.delete_credentials(user.user_id, connector_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail="No credentials found")
        
        # Commit the delete
        await session.commit()
        
        return {"status": "success", "message": "Credentials deleted"}


class TestConnectionRequest(BaseModel):
    """Test connection with optional credentials (Task 29)"""
    credentials: Optional[Dict[str, str]] = None  # Test with these credentials (not saved)
    use_stored_credentials: bool = True  # If True, use user's stored credentials


class TestConnectionResponse(BaseModel):
    """Test connection response (Task 29)"""
    success: bool
    message: str
    response_time_ms: Optional[int] = None
    tested_endpoint: Optional[str] = None
    status_code: Optional[int] = None
    error_detail: Optional[str] = None


@router.post("/{connector_id}/test-connection", response_model=TestConnectionResponse)
async def test_connector_connection(
    connector_id: str,
    request: TestConnectionRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Test connection to a connector (Task 29).
    
    Attempts to call a safe test endpoint (GET) to verify:
    - Base URL is correct
    - Credentials work
    - Network connectivity is good
    
    Can test with:
    - Stored credentials (use_stored_credentials=True)
    - New credentials before saving (provide credentials dict)
    
    Implementation:
        Uses shared OpenAPIService for endpoint discovery and testing.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.endpoint_testing import OpenAPIService
    from meho_openapi.user_credentials import UserCredentialRepository
    import logging
    
    logger = logging.getLogger(__name__)
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Use shared service for connector operations
        service = OpenAPIService(session)
        
        # Get and validate connector
        connector = await service.get_connector(connector_id, user.tenant_id)
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Find a safe test endpoint using service
        test_endpoint = await service.find_test_endpoint(connector_id)
        
        if not test_endpoint:
            return TestConnectionResponse(
                success=False,
                message="No GET endpoints available to test",
                error_detail="Upload an OpenAPI spec first"
            )
        
        # Handle custom credentials (test before saving)
        # NOTE: When testing with custom credentials, we can't use the standard
        # service.test_endpoint() because it uses stored credentials.
        # We need to call HTTP client directly with provided credentials.
        if request.credentials:
            from meho_openapi.http_client import GenericHTTPClient
            import time
            
            client = GenericHTTPClient(timeout=10.0)
            connector_schema = service.connector_to_schema(connector)
            
            start_time = time.time()
            try:
                logger.info(f"Testing connection with custom credentials to {connector.base_url}{test_endpoint.path}")
                status_code, response_data = await client.call_endpoint(
                    connector=connector_schema,
                    endpoint=test_endpoint,
                    path_params={},
                    query_params={},
                    body=None,
                    user_credentials=request.credentials
                )
                duration_ms = int((time.time() - start_time) * 1000)
                
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                return TestConnectionResponse(
                    success=False,
                    message="Connection failed",
                    response_time_ms=duration_ms,
                    tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                    error_detail=str(e)[:200]
                )
        
        elif request.use_stored_credentials:
            # Check if credentials exist for USER_PROVIDED strategy
            if connector.credential_strategy != "SYSTEM":
                cred_repo = UserCredentialRepository(session)
                user_creds = await cred_repo.get_credentials(user.user_id, connector_id)
                if not user_creds:
                    return TestConnectionResponse(
                        success=False,
                        message="No credentials configured",
                        error_detail="Please provide credentials or save them first"
                    )
            
            # Use standard service.test_endpoint() with stored credentials
            logger.info(f"Testing connection with stored credentials to {connector.base_url}{test_endpoint.path}")
            result = await service.test_endpoint(
                user_context=user,
                connector_id=connector_id,
                endpoint_id=str(test_endpoint.id)
            )
            
            status_code = result.status_code or 0
            response_data = result.data
            duration_ms = int(result.duration_ms or 0)
            
            if not result.success:
                return TestConnectionResponse(
                    success=False,
                    message=result.error or "Connection failed",
                    response_time_ms=duration_ms,
                    tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                    error_detail=result.error
                )
        else:
            # No credentials - test without auth
            result = await service.test_endpoint(
                user_context=user,
                connector_id=connector_id,
                endpoint_id=str(test_endpoint.id)
            )
            status_code = result.status_code or 0
            response_data = result.data
            duration_ms = int(result.duration_ms or 0)
            
            if not result.success:
                return TestConnectionResponse(
                    success=False,
                    message=result.error or "Connection failed",
                    response_time_ms=duration_ms,
                    tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                    error_detail=result.error
                )
        
        # Interpret results
        if 200 <= status_code < 400:
            return TestConnectionResponse(
                success=True,
                message=f"Connection successful! Endpoint returned {status_code}",
                response_time_ms=duration_ms,
                tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                status_code=status_code
            )
        elif status_code in [401, 403]:
            return TestConnectionResponse(
                success=False,
                message="Authentication failed",
                response_time_ms=duration_ms,
                tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                status_code=status_code,
                error_detail="Check your credentials"
            )
        else:
            return TestConnectionResponse(
                success=False,
                message=f"Endpoint returned error {status_code}",
                response_time_ms=duration_ms,
                tested_endpoint=f"{test_endpoint.method} {test_endpoint.path}",
                status_code=status_code,
                error_detail=str(response_data)[:200] if response_data else ""
            )


class TestAuthRequest(BaseModel):
    """Test authentication flow"""
    credentials: Optional[Dict[str, str]] = None  # For USER_PROVIDED strategy


class TestAuthResponse(BaseModel):
    """Test authentication response"""
    success: bool
    message: str
    auth_type: str
    session_token_obtained: Optional[bool] = None
    session_expires_at: Optional[datetime] = None
    error_detail: Optional[str] = None
    # Debug info
    request_url: Optional[str] = None
    request_method: Optional[str] = None
    response_status: Optional[int] = None
    response_time_ms: Optional[int] = None


@router.post("/{connector_id}/test-auth", response_model=TestAuthResponse)
async def test_connector_auth(
    connector_id: str,
    request: TestAuthRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Test authentication flow for a connector.
    
    For SESSION auth: Tests login endpoint and returns session token info
    For BASIC/API_KEY/OAUTH2: Validates credentials are configured
    For NONE: Returns success (no auth needed)
    
    This is separate from test-connection to allow testing auth setup
    without making actual API calls.
    """
    import time
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.session_manager import SessionManager
    
    start_time = time.time()
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        cred_repo = UserCredentialRepository(session)
        
        # Get connector
        connector = await connector_repo.get_connector(connector_id, user.tenant_id)
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        logger.info(f"Testing auth for connector {connector.name} (auth_type={connector.auth_type})")
        
        try:
            if connector.auth_type == "NONE":
                return TestAuthResponse(
                    success=True,
                    message="No authentication required",
                    auth_type=connector.auth_type
                )
            
            elif connector.auth_type == "SESSION":
                # Test SESSION auth by attempting login
                # If no credentials provided in request, fetch stored credentials
                credentials_to_use = request.credentials
                if not credentials_to_use:
                    logger.info(f"No credentials in request, fetching stored credentials for user {user.user_id}")
                    stored_cred = await cred_repo.get_credentials(user.user_id, connector_id)
                    if not stored_cred:
                        return TestAuthResponse(
                            success=False,
                            message="No credentials configured for this connector",
                            auth_type=connector.auth_type,
                            error_detail="Please configure credentials in the Credentials tab"
                        )
                    # Use stored credentials (already decrypted dict)
                    credentials_to_use = stored_cred
                    logger.info(f"Using stored credentials for SESSION auth")
                
                if not credentials_to_use:
                    return TestAuthResponse(
                        success=False,
                        message="Credentials required for SESSION auth",
                        auth_type=connector.auth_type,
                        error_detail="Please provide username and password"
                    )
                
                # Handle SOAP session auth differently
                if connector.connector_type == "soap":
                    # SOAP connectors use WSDL-based login (e.g., VMware VIM API Login operation)
                    try:
                        from meho_openapi.soap.client import VMwareSOAPClient
                        from meho_openapi.soap.models import SOAPConnectorConfig, SOAPAuthType
                        
                        protocol_config = connector.protocol_config or {}
                        soap_config = SOAPConnectorConfig(
                            wsdl_url=protocol_config.get("wsdl_url", ""),
                            auth_type=SOAPAuthType.SESSION,
                            username=credentials_to_use.get("username"),
                            password=credentials_to_use.get("password"),
                            login_operation="Login",
                            logout_operation="Logout",
                            verify_ssl=protocol_config.get("verify_ssl", True),
                            timeout=protocol_config.get("timeout", 30),
                        )
                        
                        # Use context manager - connect() handles login automatically
                        async with VMwareSOAPClient(soap_config) as soap_client:
                            # If we get here, login was successful
                            # The session_token is set during connect()
                            if soap_client.session_token:
                                duration_ms = int((time.time() - start_time) * 1000)
                                return TestAuthResponse(
                                    success=True,
                                    message="SOAP session authentication successful",
                                    auth_type=connector.auth_type,
                                    session_token_obtained=True,
                                    response_time_ms=duration_ms
                                )
                            else:
                                raise Exception("Login succeeded but no session token returned")
                    except Exception as e:
                        duration_ms = int((time.time() - start_time) * 1000)
                        logger.error(f"❌ SOAP SESSION auth test failed: {e}")
                        return TestAuthResponse(
                            success=False,
                            message="SOAP authentication failed",
                            auth_type=connector.auth_type,
                            error_detail=str(e)[:200],
                            response_time_ms=duration_ms
                        )
                
                # Handle VMware session auth (pyvmomi)
                elif connector.connector_type == "vmware":
                    try:
                        from meho_openapi.connectors.vmware import VMwareConnector
                        
                        protocol_config = connector.protocol_config or {}
                        vmware = VMwareConnector(
                            connector_id=connector_id,
                            config=protocol_config,
                            credentials=credentials_to_use,
                        )
                        
                        await vmware.connect()
                        is_connected = await vmware.test_connection()
                        await vmware.disconnect()
                        
                        duration_ms = int((time.time() - start_time) * 1000)
                        
                        if is_connected:
                            return TestAuthResponse(
                                success=True,
                                message="vCenter connection successful",
                                auth_type=connector.auth_type,
                                session_token_obtained=True,
                                response_time_ms=duration_ms
                            )
                        else:
                            return TestAuthResponse(
                                success=False,
                                message="vCenter connection failed",
                                auth_type=connector.auth_type,
                                error_detail="Connection test returned false",
                                response_time_ms=duration_ms
                            )
                    except Exception as e:
                        duration_ms = int((time.time() - start_time) * 1000)
                        logger.error(f"❌ VMware SESSION auth test failed: {e}")
                        return TestAuthResponse(
                            success=False,
                            message="vCenter authentication failed",
                            auth_type=connector.auth_type,
                            error_detail=str(e)[:200],
                            response_time_ms=duration_ms
                        )
                
                else:
                    # REST SESSION auth validation
                    if not connector.login_url:
                        return TestAuthResponse(
                            success=False,
                            message="Connector not configured for SESSION auth",
                            auth_type=connector.auth_type,
                            error_detail="login_url not configured"
                        )
                    
                    if not connector.login_config:
                        return TestAuthResponse(
                            success=False,
                            message="Connector not configured for SESSION auth",
                            auth_type=connector.auth_type,
                            error_detail="login_config not configured"
                        )
                    
                    # Attempt login
                    session_manager = SessionManager()
                    
                    # Build login URL for debugging
                    from urllib.parse import urljoin
                    login_url = urljoin(connector.base_url, connector.login_url.lstrip('/'))
                    login_method = connector.login_method or "POST"
                    
                    try:
                        session_token, refresh_token, expires_at, refresh_expires_at, session_state = await session_manager.login(
                            connector=connector,
                            credentials=credentials_to_use
                        )
                        
                        duration_ms = int((time.time() - start_time) * 1000)
                        
                        logger.info(f"✅ SESSION auth test successful for {connector.name}")
                        logger.info(f"   Session token obtained: {session_token[:20]}...")
                        logger.info(f"   Expires at: {expires_at}")
                        if refresh_token:
                            logger.info(f"   Refresh token obtained: {refresh_token[:20]}...")
                            if refresh_expires_at:
                                logger.info(f"   Refresh expires at: {refresh_expires_at}")
                            else:
                                logger.info(f"   Refresh token has no expiry")
                        logger.info(f"   Duration: {duration_ms}ms")
                        
                        message = f"Authentication successful (session valid for {(expires_at - datetime.utcnow()).total_seconds():.0f}s)"
                        if refresh_token:
                            if refresh_expires_at:
                                refresh_valid_secs = (refresh_expires_at - datetime.utcnow()).total_seconds()
                                message += f", refresh token valid for {refresh_valid_secs:.0f}s"
                            else:
                                message += ", refresh token available"
                        
                        return TestAuthResponse(
                            success=True,
                            message=message,
                            auth_type=connector.auth_type,
                            session_token_obtained=True,
                            session_expires_at=expires_at,
                            request_url=login_url,
                            request_method=login_method,
                            response_status=200,
                            response_time_ms=duration_ms
                        )
                    
                    except ValueError as e:
                        duration_ms = int((time.time() - start_time) * 1000)
                        logger.error(f"❌ SESSION auth test failed: {e}")
                        return TestAuthResponse(
                            success=False,
                            message="Authentication failed",
                            auth_type=connector.auth_type,
                            session_token_obtained=False,
                            error_detail=str(e)[:500],
                            request_url=login_url,
                            request_method=login_method,
                            response_time_ms=duration_ms
                        )
            
            elif connector.auth_type in ["BASIC", "API_KEY", "OAUTH2"]:
                # For non-SESSION auth, check if credentials are configured
                credentials = request.credentials
                
                if not credentials:
                    # Check if user has stored credentials (returns decrypted dict)
                    stored_creds = await cred_repo.get_credentials(user.user_id, connector_id)
                    if stored_creds:
                        credentials = stored_creds
                        logger.info(f"Using stored {connector.auth_type} credentials for user {user.user_id}")
                
                if not credentials and connector.credential_strategy == "USER_PROVIDED":
                    return TestAuthResponse(
                        success=False,
                        message=f"Credentials required for {connector.auth_type} auth",
                        auth_type=connector.auth_type,
                        error_detail="Please provide credentials"
                    )
                
                # For SYSTEM strategy, check auth_config
                if connector.credential_strategy == "SYSTEM" and not connector.auth_config:
                    return TestAuthResponse(
                        success=False,
                        message=f"Connector not configured with {connector.auth_type} credentials",
                        auth_type=connector.auth_type,
                        error_detail="auth_config is empty"
                    )
                
                # Validate credentials have required fields
                creds = credentials or connector.auth_config or {}
                
                if connector.auth_type == "BASIC":
                    if not creds.get('username') or not creds.get('password'):
                        return TestAuthResponse(
                            success=False,
                            message="BASIC auth requires username and password",
                            auth_type=connector.auth_type,
                            error_detail="Missing username or password"
                        )
                
                elif connector.auth_type == "API_KEY":
                    if not creds.get('api_key'):
                        return TestAuthResponse(
                            success=False,
                            message="API_KEY auth requires api_key",
                            auth_type=connector.auth_type,
                            error_detail="Missing api_key"
                        )
                
                elif connector.auth_type == "OAUTH2":
                    if not creds.get('access_token'):
                        return TestAuthResponse(
                            success=False,
                            message="OAUTH2 auth requires access_token",
                            auth_type=connector.auth_type,
                            error_detail="Missing access_token"
                        )
                
                return TestAuthResponse(
                    success=True,
                    message=f"{connector.auth_type} credentials configured",
                    auth_type=connector.auth_type
                )
            
            else:
                return TestAuthResponse(
                    success=False,
                    message=f"Unknown auth type: {connector.auth_type}",
                    auth_type=connector.auth_type,
                    error_detail="Unsupported auth type"
                )
        
        except Exception as e:
            logger.error(f"❌ Auth test failed: {e}", exc_info=True)
            return TestAuthResponse(
                success=False,
                message="Auth test failed",
                auth_type=connector.auth_type,
                error_detail=str(e)[:200]
            )


# ============================================================================
# Task 22: Endpoint Management Routes
# ============================================================================

@router.patch("/{connector_id}", response_model=ConnectorResponse)
async def update_connector(
    connector_id: str,
    request: UpdateConnectorRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Update connector configuration and safety policies (Task 22 + Session 54/55).
    
    Allows updating:
    - Basic info (name, description, base_url)
    - Safety policies (allowed/blocked methods)
    - Default safety level for new endpoints
    - SESSION auth configuration (login_url, login_method, login_config)
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    from meho_openapi.schemas import ConnectorUpdate
    
    logger.info(f"Updating connector {connector_id} for tenant {user.tenant_id}")
    logger.info(f"Update request: {request.model_dump(exclude_unset=True)}")
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        repo = ConnectorRepository(session)
        
        update_data = request.model_dump(exclude_unset=True)
        logger.info(f"Creating ConnectorUpdate with data: {update_data}")
        
        try:
            update = ConnectorUpdate(**update_data)
            logger.info(f"ConnectorUpdate created successfully")
        except Exception as e:
            logger.error(f"Failed to create ConnectorUpdate: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid update data: {str(e)}")
        
        connector = await repo.update_connector(connector_id, update, tenant_id=user.tenant_id)
        
        if not connector:
            logger.error(f"Connector {connector_id} not found for tenant {user.tenant_id}")
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Commit the transaction
        await session.commit()
        
        logger.info(f"Connector {connector_id} updated successfully")
        return ConnectorResponse(**connector.model_dump())


@router.get("/{connector_id}/endpoints", response_model=List[EndpointResponse])
async def list_endpoints(
    connector_id: str,
    method: Optional[str] = Query(None),
    is_enabled: Optional[bool] = Query(None),
    safety_level: Optional[str] = Query(None),
    tags: Optional[str] = Query(None, description="Comma-separated tags"),
    search: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user)
):
    """
    List endpoints for a connector with filters (Task 22).
    
    Supports filtering by:
    - HTTP method (GET, POST, etc.)
    - Enabled status
    - Safety level
    - Tags
    - Search text (in description/path)
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import EndpointDescriptorRepository
    from meho_openapi.schemas import EndpointFilter
    
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
            limit=limit
        )
        
        endpoints = await repo.list_endpoints(filter_obj)
        
        return [EndpointResponse(**ep.model_dump()) for ep in endpoints]


# ============================================================================
# TASK-97: VMware/Generic Operations Routes
# ============================================================================

class ConnectorOperationResponse(BaseModel):
    """Response for a connector operation (VMware, etc.)"""
    operation_id: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    parameters: List[Dict[str, Any]] = []
    example: Optional[str] = None


@router.get("/{connector_id}/operations", response_model=List[ConnectorOperationResponse])
async def list_connector_operations(
    connector_id: str,
    search: Optional[str] = Query(None, description="Search in operation names/descriptions"),
    category: Optional[str] = Query(None, description="Filter by category (compute, storage, network, etc.)"),
    limit: int = Query(200, ge=1, le=500),
    user: UserContext = Depends(get_current_user)
):
    """
    List operations for a typed connector (TASK-97).
    
    Returns operation definitions for connectors like VMware.
    For REST connectors, use /endpoints instead.
    For SOAP connectors, use /soap-operations instead.
    
    Supports:
    - Text search in operation names/descriptions
    - Category filtering (compute, storage, network, cluster, etc.)
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, ConnectorOperationRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        op_repo = ConnectorOperationRepository(session)
        
        if search:
            operations = await op_repo.search_operations(connector_id, search, limit)
        elif category:
            operations = await op_repo.list_operations(
                connector_id=connector_id,
                category=category,
                limit=limit
            )
        else:
            operations = await op_repo.list_operations(
                connector_id=connector_id,
                limit=limit
            )
        
        return [
            ConnectorOperationResponse(
                operation_id=op.operation_id,
                name=op.name,
                description=op.description,
                category=op.category,
                parameters=op.parameters or [],
                example=op.example,
            )
            for op in operations
        ]


class SyncOperationsResponse(BaseModel):
    """Response for syncing connector operations."""
    connector_id: str
    operations_added: int
    operations_updated: int
    operations_total: int
    message: str


@router.post("/{connector_id}/operations/sync", response_model=SyncOperationsResponse)
async def sync_connector_operations(
    connector_id: str,
    user: UserContext = Depends(get_current_user)
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
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, ConnectorOperationRepository
    from meho_openapi.schemas import ConnectorOperationCreate
    from meho_openapi.connectors.vmware import VMWARE_OPERATIONS
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if connector.connector_type != "vmware":
            raise HTTPException(
                status_code=400, 
                detail=f"Operation sync only supported for VMware connectors, got: {connector.connector_type}"
            )
        
        op_repo = ConnectorOperationRepository(session)
        
        # Get existing operations
        existing_ops = await op_repo.list_operations(connector_id=connector_id, limit=1000)
        existing_op_ids = {op.operation_id for op in existing_ops}
        
        added = 0
        updated = 0
        
        for op in VMWARE_OPERATIONS:
            search_content = f"{op.name} {op.operation_id} {op.description} {op.category}"
            
            if op.operation_id not in existing_op_ids:
                # Add new operation
                await op_repo.create_operation(ConnectorOperationCreate(
                    connector_id=connector_id,
                    tenant_id=user.tenant_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=[p for p in op.parameters],
                    example=op.example,
                    search_content=search_content,
                ))
                added += 1
            else:
                # Update existing operation
                await op_repo.update_operation(
                    connector_id=connector_id,
                    operation_id=op.operation_id,
                    name=op.name,
                    description=op.description,
                    category=op.category,
                    parameters=[p for p in op.parameters],
                    example=op.example,
                    search_content=search_content,
                )
                updated += 1
        
        await session.commit()
        
        return SyncOperationsResponse(
            connector_id=connector_id,
            operations_added=added,
            operations_updated=updated,
            operations_total=len(VMWARE_OPERATIONS),
            message=f"Synced {added} new operations, updated {updated} existing operations."
        )


# ============================================================================
# TASK-98: REST Schema Types Routes
# ============================================================================

class SchemaTypeResponse(BaseModel):
    """Response for an OpenAPI schema type (TASK-98)"""
    type_name: str
    description: Optional[str] = None
    category: Optional[str] = None
    properties: List[Dict[str, Any]] = []


@router.get("/{connector_id}/types", response_model=List[SchemaTypeResponse])
async def list_connector_types(
    connector_id: str,
    search: Optional[str] = Query(None, description="Search in type names/descriptions"),
    category: Optional[str] = Query(None, description="Filter by category (model, request, response, error)"),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user)
):
    """
    List schema types for a connector (TASK-98).
    
    Returns type definitions extracted from OpenAPI components/schemas.
    Works for REST connectors. For SOAP connectors, use /soap-types instead.
    
    Supports:
    - Text search in type names/descriptions
    - Category filtering (model, request, response, error, collection)
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, ConnectorTypeRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Read types from database
        type_repo = ConnectorTypeRepository(session)
        
        if search:
            # Use search if query provided
            types = await type_repo.search_types(connector_id, search, limit)
        else:
            # List with filters
            types = await type_repo.list_types(
                connector_id=connector_id,
                category=category,
                limit=limit
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
    connector_id: str,
    type_name: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Get a specific schema type definition (TASK-98).
    
    Returns detailed type information including all properties.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, ConnectorTypeRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Get type from database
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


@router.patch("/{connector_id}/endpoints/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(
    connector_id: str,
    endpoint_id: str,
    request: UpdateEndpointRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Update endpoint configuration (Task 22).
    
    Allows editing:
    - Enable/disable endpoint
    - Safety level (safe/caution/dangerous)
    - Approval requirement
    - Custom description (enhanced docs)
    - Admin notes (internal)
    - Usage examples
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import EndpointDescriptorRepository
    from meho_openapi.schemas import EndpointUpdate
    
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
    user: UserContext = Depends(get_current_user)
):
    """
    Test an endpoint with live API call (Task 22).
    
    Makes a real HTTP request to the endpoint with provided parameters.
    Useful for verifying connectivity and testing before agent use.
    
    Implementation:
        Uses shared OpenAPIService for consistent endpoint testing.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.endpoint_testing import OpenAPIService
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Use shared service for endpoint testing
        service = OpenAPIService(session)
        result = await service.test_endpoint(
            user_context=user,
            connector_id=connector_id,
            endpoint_id=endpoint_id,
            path_params=request.path_params,
            query_params=request.query_params,
            body=request.body
        )
        
        if result.success:
            return TestEndpointResponse(
                status_code=result.status_code or 200,
                headers={},  # Service returns data, not raw headers
                body=result.data,
                duration_ms=int(result.duration_ms or 0)
            )
        else:
            return TestEndpointResponse(
                status_code=result.status_code or 500,
                headers={},
                body=None,
                duration_ms=int(result.duration_ms or 0),
                error=result.error
            )


@router.delete("/{connector_id}")
async def delete_connector(
    connector_id: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Delete a connector and all related data.
    
    This will delete:
    - Connector configuration (database cascade deletes specs, endpoints, credentials)
    - Knowledge chunks from OpenAPI ingestion
    
    Returns 204 No Content on success, 404 if not found.
    """
    from meho_api.database import create_openapi_session_maker, create_knowledge_session_maker
    from meho_openapi.repository import ConnectorRepository
    from meho_knowledge.repository import KnowledgeRepository
    from meho_knowledge.schemas import KnowledgeChunkFilter
    
    openapi_session_maker = create_openapi_session_maker()
    
    async with openapi_session_maker() as session:
        repo = ConnectorRepository(session)
        
        # Check if connector exists and belongs to tenant
        connector = await repo.get_connector(connector_id, tenant_id=user.tenant_id)
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        connector_name = connector.name
        
        # Step 1: Delete knowledge chunks created from OpenAPI or SOAP ingestion
        # These are tagged with source_uri: openapi://{connector_id}/... or soap://{connector_id}/...
        knowledge_deleted = 0
        try:
            from meho_knowledge.models import KnowledgeChunkModel
            from sqlalchemy import select, or_
            
            knowledge_session_maker = create_knowledge_session_maker()
            async with knowledge_session_maker() as knowledge_session:
                knowledge_repo = KnowledgeRepository(knowledge_session)
                
                # Find all chunks with source_uri starting with openapi://{connector_id}/ or soap://{connector_id}/
                openapi_prefix = f"openapi://{connector_id}/"
                soap_prefix = f"soap://{connector_id}/"
                
                # Use SQL LIKE query for efficient prefix matching (supports both REST and SOAP)
                result = await knowledge_session.execute(
                    select(KnowledgeChunkModel).where(
                        KnowledgeChunkModel.tenant_id == user.tenant_id,
                        or_(
                            KnowledgeChunkModel.source_uri.like(f"{openapi_prefix}%"),
                            KnowledgeChunkModel.source_uri.like(f"{soap_prefix}%")
                        )
                    )
                )
                connector_chunks = result.scalars().all()
                
                # Delete each chunk
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
            # Log error but continue with connector deletion
            logger.error(
                f"⚠️ Failed to delete knowledge chunks for connector {connector_name}: {e}",
                exc_info=True
            )
        
        # Step 2: Delete blob storage files (OpenAPI specs)
        storage_deleted = 0
        try:
            from meho_knowledge.object_storage import ObjectStorage
            from meho_openapi.repository import OpenAPISpecRepository
            
            object_storage = ObjectStorage()
            spec_repo = OpenAPISpecRepository(session)
            
            # Get OpenAPI spec to find storage_uri
            openapi_spec = await spec_repo.get_spec_by_connector(connector_id)
            
            if openapi_spec and openapi_spec.storage_uri:
                # Extract key from s3://bucket/key format
                storage_key = openapi_spec.storage_uri.split('/', 3)[-1]  # Gets "key" from "s3://bucket/key"
                
                try:
                    object_storage.delete_document(storage_key)
                    storage_deleted = 1
                    logger.info(f"🗑️ Deleted OpenAPI spec file from storage: {storage_key}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete storage file {storage_key}: {e}")
        except Exception as e:
            # Log error but continue with connector deletion
            logger.error(
                f"⚠️ Failed to delete blob storage files for connector {connector_name}: {e}",
                exc_info=True
            )
        
        # Step 3: Delete connector (cascade deletes specs, endpoints, credentials)
        deleted = await repo.delete_connector(connector_id, tenant_id=user.tenant_id)
        
        if not deleted:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Commit the delete transaction
        await session.commit()
        
        logger.info(
            f"🗑️ Connector deleted: {connector_name} (id={connector_id}) by user {user.user_id}. "
            f"Knowledge chunks deleted: {knowledge_deleted}"
        )
        
        return {
            "message": "Connector deleted successfully",
            "knowledge_chunks_deleted": knowledge_deleted
        }


# ============================================================================
# TASK-75: SOAP/WSDL Support Routes
# ============================================================================

class IngestWSDLRequest(BaseModel):
    """Request to ingest a WSDL file"""
    wsdl_url: str = Field(..., description="URL or path to WSDL file")


class SOAPOperationResponse(BaseModel):
    """SOAP operation response"""
    name: str
    service_name: str
    port_name: str
    operation_name: str
    description: Optional[str] = None
    soap_action: Optional[str] = None
    style: str = "document"
    namespace: str = ""
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    is_enabled: bool = True


class IngestWSDLResponse(BaseModel):
    """Response from WSDL ingestion"""
    message: str
    wsdl_url: str
    operations_count: int
    types_count: int = 0  # TASK-96: SOAP type definitions count
    services: List[str]
    ports: List[str]


class CallSOAPRequest(BaseModel):
    """Request to call a SOAP operation"""
    params: Dict[str, Any] = Field(default_factory=dict, description="Operation parameters")
    service_name: Optional[str] = None
    port_name: Optional[str] = None


class CallSOAPResponse(BaseModel):
    """Response from SOAP operation call"""
    success: bool
    status_code: int
    body: Dict[str, Any]
    fault_code: Optional[str] = None
    fault_string: Optional[str] = None
    duration_ms: Optional[float] = None


@router.post("/{connector_id}/wsdl", response_model=IngestWSDLResponse)
async def ingest_wsdl(
    connector_id: str,
    request: IngestWSDLRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Ingest a WSDL file to discover SOAP operations (TASK-75).
    
    This:
    1. Parses the WSDL file using zeep
    2. Extracts all SOAP operations with their schemas
    3. Stores operations for search and execution
    4. Ingests operations into knowledge base for natural language search
    
    Requirements:
    - Connector must have connector_type='soap'
    - WSDL URL must be accessible (or file path on server)
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    from meho_openapi.schemas import ConnectorUpdate
    from meho_openapi.soap import SOAPSchemaIngester
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        # Verify connector exists and user has access
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        # Check protocol is SOAP
        if getattr(connector, 'connector_type', 'rest') != 'soap':
            raise HTTPException(
                status_code=400,
                detail=f"Connector type is '{getattr(connector, 'connector_type', 'rest')}', expected 'soap'. "
                       f"Create a SOAP connector to use WSDL ingestion."
            )
        
        # Ingest WSDL
        try:
            from meho_openapi.soap.models import SOAPConnectorConfig
            
            # Get verify_ssl from connector's protocol_config
            protocol_config = getattr(connector, 'protocol_config', {}) or {}
            verify_ssl = protocol_config.get('verify_ssl', True)
            timeout = protocol_config.get('timeout', 30)
            
            # Create ingester with SSL config
            soap_config = SOAPConnectorConfig(
                wsdl_url=request.wsdl_url,
                verify_ssl=verify_ssl,
                timeout=timeout,
            )
            ingester = SOAPSchemaIngester(config=soap_config)
            
            # Get credentials if needed for WSDL access
            auth = None
            if connector.credential_strategy == "USER_PROVIDED":
                from meho_openapi.user_credentials import UserCredentialRepository
                cred_repo = UserCredentialRepository(session)
                user_creds = await cred_repo.get_credentials(user.user_id, connector_id)
                if user_creds:
                    auth = user_creds
            
            # TASK-96: ingest_wsdl now returns (operations, metadata, type_definitions)
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
            
            # Update connector's protocol_config with WSDL URL
            protocol_config = getattr(connector, 'protocol_config', {}) or {}
            protocol_config['wsdl_url'] = request.wsdl_url
            protocol_config['services'] = metadata.services
            protocol_config['ports'] = metadata.ports
            protocol_config['operation_count'] = len(operations)
            
            await connector_repo.update_connector(
                connector_id,
                ConnectorUpdate(protocol_config=protocol_config),
                tenant_id=user.tenant_id
            )
            
            # Store operations and types in DB tables
            from meho_openapi.repository import SoapOperationRepository, SoapTypeRepository
            from meho_openapi.schemas import SoapOperationDescriptorCreate, SoapTypeDescriptorCreate
            
            soap_op_repo = SoapOperationRepository(session)
            soap_type_repo = SoapTypeRepository(session)
            
            # Delete existing operations and types (re-ingestion case)
            await soap_op_repo.delete_by_connector(connector_id)
            await soap_type_repo.delete_by_connector(connector_id)
            
            # Convert SOAPOperation to SoapOperationDescriptorCreate
            op_creates = []
            for op in operations:
                search_content = f"{op.name} {op.operation_name} {op.service_name} {op.description or ''}"
                op_creates.append(SoapOperationDescriptorCreate(
                    connector_id=str(connector.id),
                    tenant_id=user.tenant_id,
                    service_name=op.service_name,
                    port_name=op.port_name,
                    operation_name=op.operation_name,
                    name=op.name,
                    description=op.description,
                    soap_action=op.soap_action,
                    namespace=op.namespace,
                    style=op.style.value if hasattr(op.style, 'value') else str(op.style),
                    input_schema=op.input_schema or {},
                    output_schema=op.output_schema or {},
                    protocol_details=op.protocol_details or {},
                    search_content=search_content,
                ))
            
            # Convert SOAPTypeDefinition to SoapTypeDescriptorCreate
            type_creates = []
            for type_def in type_definitions:
                prop_names = " ".join(p.name for p in type_def.properties)
                search_content = f"{type_def.name} {type_def.base_type or ''} {prop_names}"
                type_creates.append(SoapTypeDescriptorCreate(
                    connector_id=str(connector.id),
                    tenant_id=user.tenant_id,
                    type_name=type_def.name,
                    namespace=type_def.namespace,
                    base_type=type_def.base_type,
                    properties=[p.model_dump() for p in type_def.properties],  # type: ignore[misc]
                    description=type_def.description,
                    search_content=search_content,
                ))
            
            # Bulk create operations and types
            ops_count = await soap_op_repo.create_operations_bulk(op_creates)
            types_count = await soap_type_repo.create_types_bulk(type_creates)
            
            await session.commit()
            
            logger.info(f"✅ Stored {ops_count} SOAP operations and {types_count} types in database")
            
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
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"❌ WSDL ingestion failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"WSDL ingestion failed: {e}")


@router.get("/{connector_id}/soap-operations", response_model=List[SOAPOperationResponse])
async def list_soap_operations(
    connector_id: str,
    search: Optional[str] = Query(None, description="Search in operation names/descriptions"),
    service: Optional[str] = Query(None, description="Filter by service name"),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user)
):
    """
    List SOAP operations for a connector.
    
    Returns operations from the database (ingested from WSDL).
    Supports filtering by service name and text search.
    
    TASK-96: Now reads from soap_operation_descriptor table instead of re-parsing WSDL.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, SoapOperationRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if getattr(connector, 'connector_type', 'rest') != 'soap':
            raise HTTPException(
                status_code=400,
                detail="Connector is not a SOAP connector"
            )
        
        # Read operations from database
        soap_op_repo = SoapOperationRepository(session)
        
        if search:
            # Use search if query provided
            operations = await soap_op_repo.search_operations(connector_id, search, limit)
        else:
            # List with filters
            operations = await soap_op_repo.list_operations(
                connector_id=connector_id,
                service_name=service,
                limit=limit
            )
        
        if not operations:
            # Check if WSDL has been ingested
            protocol_config = getattr(connector, 'protocol_config', {}) or {}
            if not protocol_config.get('wsdl_url'):
                raise HTTPException(
                    status_code=400,
                    detail="No WSDL has been ingested for this connector. "
                           "Use POST /{connector_id}/wsdl first."
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


class SOAPTypeResponse(BaseModel):
    """Response for a SOAP type definition"""
    type_name: str
    namespace: Optional[str] = None
    base_type: Optional[str] = None
    properties: List[Dict[str, Any]] = []
    description: Optional[str] = None


@router.get("/{connector_id}/soap-types", response_model=List[SOAPTypeResponse])
async def list_soap_types(
    connector_id: str,
    search: Optional[str] = Query(None, description="Search in type names/descriptions"),
    base_type: Optional[str] = Query(None, description="Filter by base type"),
    limit: int = Query(100, ge=1, le=500),
    user: UserContext = Depends(get_current_user)
):
    """
    List SOAP types for a connector (TASK-96).
    
    Returns type definitions from the database (ingested from WSDL).
    Supports filtering by base type and text search.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, SoapTypeRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if getattr(connector, 'connector_type', 'rest') != 'soap':
            raise HTTPException(
                status_code=400,
                detail="Connector is not a SOAP connector"
            )
        
        # Read types from database
        soap_type_repo = SoapTypeRepository(session)
        
        if search:
            # Use search if query provided
            types = await soap_type_repo.search_types(connector_id, search, limit)
        else:
            # List with filters
            types = await soap_type_repo.list_types(
                connector_id=connector_id,
                base_type=base_type,
                limit=limit
            )
        
        if not types:
            # Check if WSDL has been ingested
            protocol_config = getattr(connector, 'protocol_config', {}) or {}
            if not protocol_config.get('wsdl_url'):
                raise HTTPException(
                    status_code=400,
                    detail="No WSDL has been ingested for this connector. "
                           "Use POST /{connector_id}/wsdl first."
                )
        
        return [
            SOAPTypeResponse(
                type_name=t.type_name,
                namespace=t.namespace,
                base_type=t.base_type,
                # Convert SoapPropertySchema objects to dicts
                properties=[
                    p.model_dump() if hasattr(p, 'model_dump') else p  # type: ignore[misc]
                    for p in (t.properties or [])
                ],
                description=t.description,
            )
            for t in types
        ]


@router.get("/{connector_id}/soap-types/{type_name}", response_model=SOAPTypeResponse)
async def get_soap_type(
    connector_id: str,
    type_name: str,
    user: UserContext = Depends(get_current_user)
):
    """
    Get a specific SOAP type definition (TASK-96).
    
    Returns detailed type information including all properties.
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository, SoapTypeRepository
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if getattr(connector, 'connector_type', 'rest') != 'soap':
            raise HTTPException(
                status_code=400,
                detail="Connector is not a SOAP connector"
            )
        
        # Get type from database
        soap_type_repo = SoapTypeRepository(session)
        type_def = await soap_type_repo.get_type_by_name(connector_id, type_name)
        
        if not type_def:
            raise HTTPException(status_code=404, detail=f"Type '{type_name}' not found")
        
        return SOAPTypeResponse(
            type_name=type_def.type_name,
            namespace=type_def.namespace,
            base_type=type_def.base_type,
            # Convert SoapPropertySchema objects to dicts
            properties=[
                p.model_dump() if hasattr(p, 'model_dump') else p  # type: ignore[misc]
                for p in (type_def.properties or [])
            ],
            description=type_def.description,
        )


@router.post("/{connector_id}/soap-operations/{operation_name}/call", response_model=CallSOAPResponse)
async def call_soap_operation(
    connector_id: str,
    operation_name: str,
    request: CallSOAPRequest,
    user: UserContext = Depends(get_current_user)
):
    """
    Call a SOAP operation (TASK-75).
    
    Executes a SOAP operation with the provided parameters.
    For VMware VIM API, this handles session-based authentication automatically.
    
    Example:
        POST /connectors/{id}/soap-operations/RetrieveProperties/call
        {
            "params": {
                "specSet": [...]
            }
        }
    """
    from meho_api.database import create_openapi_session_maker
    from meho_openapi.repository import ConnectorRepository
    from meho_openapi.user_credentials import UserCredentialRepository
    from meho_openapi.soap import SOAPClient, VMwareSOAPClient, SOAPConnectorConfig, SOAPAuthType
    
    session_maker = create_openapi_session_maker()
    
    async with session_maker() as session:
        connector_repo = ConnectorRepository(session)
        connector = await connector_repo.get_connector(connector_id, tenant_id=user.tenant_id)
        
        if not connector:
            raise HTTPException(status_code=404, detail="Connector not found")
        
        if getattr(connector, 'connector_type', 'rest') != 'soap':
            raise HTTPException(
                status_code=400,
                detail="Connector is not a SOAP connector"
            )
        
        # Get WSDL URL and config
        protocol_config = getattr(connector, 'protocol_config', {}) or {}
        wsdl_url = protocol_config.get('wsdl_url')
        
        if not wsdl_url:
            raise HTTPException(
                status_code=400,
                detail="No WSDL has been ingested for this connector"
            )
        
        # Get user credentials
        credentials = None
        if connector.credential_strategy == "USER_PROVIDED":
            cred_repo = UserCredentialRepository(session)
            credentials = await cred_repo.get_credentials(user.user_id, connector_id)
            
            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Credentials required. Please configure credentials first."
                )
        
        # Build SOAP client config
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
        
        # Use VMware client if applicable
        is_vmware = (
            "vmware" in connector.name.lower() or
            "vim" in wsdl_url.lower() or
            "vsphere" in connector.name.lower()
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

