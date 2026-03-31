"""
Pydantic schemas for OpenAPI service.
"""
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime


# ============================================================================
# Connector Schemas
# ============================================================================

class ConnectorCreate(BaseModel):
    """Request to create a connector"""
    tenant_id: str
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    base_url: str
    
    # Connector type - single source of truth for connector classification
    connector_type: Literal["rest", "soap", "graphql", "grpc", "vmware", "kubernetes"] = "rest"
    protocol_config: Optional[Dict[str, Any]] = None  # Type-specific configuration
    
    auth_type: Literal["API_KEY", "OAUTH2", "BASIC", "NONE", "SESSION"]
    auth_config: Dict[str, Any] = Field(default_factory=dict)
    credential_strategy: Literal["SYSTEM", "USER_PROVIDED"] = "SYSTEM"
    
    # Session-based authentication configuration (for SESSION auth type)
    login_url: Optional[str] = None  # e.g., "/api/v1/auth/login"
    login_method: Optional[str] = "POST"  # POST or GET
    login_config: Optional[Dict[str, Any]] = None  # Login request/response configuration
    #
    # login_config structure (all fields optional except where noted):
    # {
    #     # Login Authentication (TASK-64)
    #     "login_auth_type": "basic" | "body",  # How to send credentials (default: "body")
    #                                            # - "basic": HTTP Basic Auth (username:password in Authorization header)
    #                                            # - "body": JSON body with credentials (default)
    #     "login_headers": {...},                # Custom headers for login request (e.g., {"vmware-use-header-authn": "test"})
    #
    #     # Body Auth (when login_auth_type="body" or not specified)
    #     "body_template": {                     # Template for login request body
    #         "username": "{{username}}",        # {{var}} will be replaced with credential values
    #         "password": "{{password}}"
    #     },
    #
    #     # Token Extraction (from login response)
    #     "token_location": "header" | "cookie" | "body",  # Where to extract token from response
    #     "token_name": "X-Auth-Token",          # Header/cookie name (for header/cookie location)
    #     "token_path": "$.value",               # JSONPath for token (for body location, e.g., "$.value" or "$.token")
    #     "header_name": "vmware-api-session-id", # Header name to use when sending token in requests
    #
    #     # Session Management
    #     "session_duration_seconds": 3600,      # Session lifetime (default: 3600 = 1 hour)
    #
    #     # Token Refresh (optional)
    #     "refresh_url": "/api/v1/auth/refresh",  # URL for token refresh
    #     "refresh_method": "POST",               # Method for refresh (default: POST)
    #     "refresh_token_path": "$.refreshToken", # JSONPath to extract refresh token from login response
    #     "refresh_token_expires_in": 86400,      # Refresh token lifetime in seconds
    #     "refresh_body_template": {              # Template for refresh request body
    #         "refreshToken": {"id": "{{refresh_token}}"}
    #     }
    # }
    #
    # Examples:
    #
    # vCenter (Basic Auth with custom headers):
    # {
    #     "login_auth_type": "basic",
    #     "login_headers": {"vmware-use-header-authn": "test"},
    #     "token_location": "body",
    #     "token_path": "$.value",
    #     "header_name": "vmware-api-session-id",
    #     "session_duration_seconds": 3600
    # }
    #
    # Standard JSON Body Auth:
    # {
    #     "login_auth_type": "body",
    #     "body_template": {"username": "{{username}}", "password": "{{password}}"},
    #     "token_location": "header",
    #     "token_name": "X-Auth-Token",
    #     "session_duration_seconds": 7200
    # }
    
    # Safety Policies (Task 22)
    allowed_methods: List[str] = Field(default=["GET", "POST", "PUT", "PATCH", "DELETE"])
    blocked_methods: List[str] = Field(default_factory=list)
    default_safety_level: Literal["safe", "caution", "dangerous"] = "safe"


class Connector(ConnectorCreate):
    """Connector with ID and metadata"""
    id: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    
    # SESSION auth fields are inherited from ConnectorCreate
    # (login_url, login_method, login_config)
    
    model_config = ConfigDict(from_attributes=True)
    
    @field_validator('id', mode='before')
    @classmethod
    def convert_uuid_to_str(cls, v: Any) -> str:
        """Convert UUID to string for id field"""
        from uuid import UUID
        if isinstance(v, UUID):
            return str(v)
        return str(v)
    
    @field_validator('auth_config', mode='before')
    @classmethod
    def ensure_auth_config_dict(cls, v: Any) -> Dict[str, Any]:
        """Ensure auth_config is a dict, not None"""
        if v is None:
            return {}
        return dict(v) if v else {}


class ConnectorUpdate(BaseModel):
    """Update connector"""
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None
    
    # Connector type - single source of truth for connector classification
    connector_type: Optional[Literal["rest", "soap", "graphql", "grpc", "vmware", "kubernetes"]] = None
    protocol_config: Optional[Dict[str, Any]] = None
    
    auth_type: Optional[str] = None
    auth_config: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None
    
    # Session auth configuration
    login_url: Optional[str] = None
    login_method: Optional[str] = None
    login_config: Optional[Dict[str, Any]] = None
    
    # Safety Policies (Task 22)
    allowed_methods: Optional[List[str]] = None
    blocked_methods: Optional[List[str]] = None
    default_safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None


# ============================================================================
# OpenAPI Spec Schemas
# ============================================================================

class OpenAPISpecCreate(BaseModel):
    """Create OpenAPI spec record"""
    connector_id: str
    storage_uri: str
    version: Optional[str] = None
    spec_version: Optional[str] = None


class OpenAPISpec(OpenAPISpecCreate):
    """OpenAPI spec with ID"""
    id: str
    created_at: datetime
    
    model_config = ConfigDict(from_attributes=True)
    
    model_config = ConfigDict(from_attributes=True)


# ============================================================================
# Endpoint Descriptor Schemas
# ============================================================================

class EndpointDescriptorCreate(BaseModel):
    """Create endpoint descriptor"""
    connector_id: str
    method: str
    path: str
    operation_id: Optional[str] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    required_params: List[str] = Field(default_factory=list)
    path_params_schema: Dict[str, Any] = Field(default_factory=dict)
    query_params_schema: Dict[str, Any] = Field(default_factory=dict)
    body_schema: Dict[str, Any] = Field(default_factory=dict)
    response_schema: Dict[str, Any] = Field(default_factory=dict)
    
    # Session 78: Explicit parameter metadata for LLM guidance
    # Structured format that clearly indicates what's required vs optional
    parameter_metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Explicit parameter requirements for LLM workflow building"
    )
    # Format:
    # {
    #   "path_params": {"required": ["id"], "optional": []},
    #   "query_params": {"required": ["limit"], "optional": ["offset", "filter"]},
    #   "body": {"required": true, "required_fields": ["name"], "optional_fields": ["description"]},
    #   "headers": {"required": [], "optional": ["X-Request-ID"]}
    # }
    
    # TASK-81: LLM instructions for schema-guided parameter collection
    llm_instructions: Optional[Dict[str, Any]] = Field(
        default=None,
        description="LLM guidance for helping users through complex parameter collection"
    )
    
    # Activation & Safety (Task 22)
    is_enabled: bool = True
    safety_level: Literal["safe", "caution", "dangerous"] = "safe"
    requires_approval: bool = False
    
    # Enhanced Documentation (Task 22)
    custom_description: Optional[str] = None
    custom_notes: Optional[str] = None
    usage_examples: Optional[Dict[str, Any]] = None


class EndpointDescriptor(EndpointDescriptorCreate):
    """Endpoint descriptor with ID"""
    id: str
    created_at: datetime
    
    # Audit Trail (Task 22)
    last_modified_by: Optional[str] = None
    last_modified_at: Optional[datetime] = None
    
    # Agent Learning (future)
    agent_notes: Optional[str] = None
    common_errors: Optional[List[Dict[str, Any]]] = None
    success_patterns: Optional[List[Dict[str, Any]]] = None
    
    model_config = ConfigDict(from_attributes=True)


class EndpointUpdate(BaseModel):
    """Update endpoint configuration (Task 22)"""
    is_enabled: Optional[bool] = None
    safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None
    requires_approval: Optional[bool] = None
    custom_description: Optional[str] = None
    custom_notes: Optional[str] = None
    usage_examples: Optional[Dict[str, Any]] = None
    # TASK-81: LLM instructions for schema-guided parameter collection
    llm_instructions: Optional[Dict[str, Any]] = None


class EndpointFilter(BaseModel):
    """Filter for searching endpoints"""
    connector_id: Optional[str] = None
    method: Optional[str] = None
    tags: Optional[List[str]] = None
    search_text: Optional[str] = None
    is_enabled: Optional[bool] = None  # Task 22: Filter by status
    safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None  # Task 22
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


# ============================================================================
# User Credential Schemas
# ============================================================================

class UserCredentialProvide(BaseModel):
    """User provides their credentials for a connector"""
    connector_id: str
    credential_type: Literal["PASSWORD", "API_KEY", "OAUTH2_TOKEN", "SESSION"]
    credentials: Dict[str, str]  # e.g., {"username": "...", "password": "..."}


class UserCredentialStatus(BaseModel):
    """Status of user's credentials"""
    connector_id: str
    connector_name: str
    has_credentials: bool
    credential_type: Optional[str] = None
    is_active: bool
    last_used_at: Optional[datetime] = None
    needs_refresh: bool = False


class UserCredentialCreate(BaseModel):
    """Create a user credential"""
    connector_id: str
    user_id: str
    credential_type: Literal["PASSWORD", "API_KEY", "OAUTH2_TOKEN", "SESSION"]
    credentials: Dict[str, str]  # e.g., {"username": "...", "password": "..."}


class UserCredential(BaseModel):
    """User credential (without exposing encrypted data)"""
    id: str
    connector_id: str
    user_id: str
    credential_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ============================================================================
# SOAP Schemas (TASK-96: SOAP Type Support)
# ============================================================================

class SoapOperationDescriptorCreate(BaseModel):
    """Create a SOAP operation descriptor"""
    connector_id: str
    tenant_id: str
    service_name: str
    port_name: str
    operation_name: str
    name: str  # Full name: "ServiceName.OperationName"
    description: Optional[str] = None
    soap_action: Optional[str] = None
    namespace: Optional[str] = None
    style: str = "document"
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    protocol_details: Dict[str, Any] = Field(default_factory=dict)
    search_content: Optional[str] = None
    is_enabled: bool = True
    safety_level: Literal["safe", "caution", "dangerous"] = "caution"
    requires_approval: bool = False


class SoapOperationDescriptor(SoapOperationDescriptorCreate):
    """SOAP operation descriptor with ID and timestamps"""
    id: str
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class SoapOperationFilter(BaseModel):
    """Filter for searching SOAP operations"""
    service_name: Optional[str] = None
    search: Optional[str] = None
    is_enabled: Optional[bool] = None
    safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None


class SoapPropertySchema(BaseModel):
    """A property on a SOAP type"""
    name: str
    type_name: str
    is_array: bool = False
    is_required: bool = False
    description: Optional[str] = None


class SoapTypeDescriptorCreate(BaseModel):
    """Create a SOAP type descriptor"""
    connector_id: str
    tenant_id: str
    type_name: str
    namespace: Optional[str] = None
    base_type: Optional[str] = None
    properties: List[SoapPropertySchema] = Field(default_factory=list)
    description: Optional[str] = None
    search_content: Optional[str] = None
    
    @field_validator('properties', mode='before')
    @classmethod
    def validate_properties(cls, v: Any) -> Any:
        """Ensure properties are properly formatted (accepts dicts or SoapPropertySchema)"""
        if isinstance(v, list):
            return [
                SoapPropertySchema(**p) if isinstance(p, dict) else p 
                for p in v
            ]
        return v


class SoapTypeDescriptor(SoapTypeDescriptorCreate):
    """SOAP type descriptor with ID and timestamps"""
    id: str
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class SoapTypeFilter(BaseModel):
    """Filter for searching SOAP types"""
    search: Optional[str] = None
    base_type: Optional[str] = None


# ============================================================================
# Typed Connector Schemas (TASK-97: VMware/Kubernetes/etc)
# ============================================================================

class ConnectorOperationCreate(BaseModel):
    """Create a typed connector operation"""
    connector_id: str
    tenant_id: str
    operation_id: str  # e.g., "list_virtual_machines"
    name: str  # e.g., "List Virtual Machines"
    description: Optional[str] = None
    category: Optional[str] = None  # e.g., "compute", "storage"
    parameters: List[Dict[str, Any]] = Field(default_factory=list)
    example: Optional[str] = None
    search_content: Optional[str] = None
    is_enabled: bool = True
    safety_level: Literal["safe", "caution", "dangerous"] = "safe"
    requires_approval: bool = False


class ConnectorOperationDescriptor(ConnectorOperationCreate):
    """Typed connector operation with ID and timestamps"""
    id: str
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class ConnectorOperationFilter(BaseModel):
    """Filter for searching connector operations"""
    category: Optional[str] = None
    search: Optional[str] = None
    is_enabled: Optional[bool] = None
    safety_level: Optional[Literal["safe", "caution", "dangerous"]] = None


class ConnectorEntityTypeCreate(BaseModel):
    """Create a typed connector entity type"""
    connector_id: str
    tenant_id: str
    type_name: str  # e.g., "VirtualMachine"
    description: Optional[str] = None
    category: Optional[str] = None  # e.g., "compute", "storage"
    properties: List[Dict[str, Any]] = Field(default_factory=list)
    search_content: Optional[str] = None


class ConnectorEntityType(ConnectorEntityTypeCreate):
    """Typed connector entity type with ID and timestamps"""
    id: str
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class ConnectorEntityTypeFilter(BaseModel):
    """Filter for searching connector entity types"""
    category: Optional[str] = None
    search: Optional[str] = None


# ============================================================================
# VMware Connector Schemas (TASK-97)
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

