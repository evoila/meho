"""
SOAP/WSDL Data Models

Pydantic models for SOAP operations, configuration, and metadata.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class SOAPAuthType(str, Enum):
    """Authentication types for SOAP connectors"""
    NONE = "none"
    BASIC = "basic"  # HTTP Basic Auth
    SESSION = "session"  # Login-based session (e.g., VMware VIM)
    WS_SECURITY = "ws_security"  # WS-Security tokens
    CERTIFICATE = "certificate"  # Client certificate


class SOAPStyle(str, Enum):
    """SOAP binding styles"""
    DOCUMENT = "document"
    RPC = "rpc"


class WSDLMetadata(BaseModel):
    """Metadata extracted from a WSDL file"""
    
    wsdl_url: str
    target_namespace: str
    services: List[str]
    ports: List[str]
    operation_count: int
    parsed_at: datetime = Field(default_factory=datetime.utcnow)


class SOAPParameter(BaseModel):
    """A parameter in a SOAP operation"""
    
    name: str
    type: str  # XSD type name
    json_type: str  # Mapped JSON Schema type
    required: bool = True
    description: Optional[str] = None
    default: Optional[Any] = None
    
    # Nested complex type schema (if applicable)
    properties: Optional[Dict[str, Any]] = None


class SOAPOperation(BaseModel):
    """A SOAP operation discovered from WSDL
    
    This is the SOAP equivalent of EndpointDescriptor for REST APIs.
    """
    
    # Identity
    id: Optional[UUID] = None
    connector_id: UUID
    tenant_id: str
    
    # Operation identification
    service_name: str
    port_name: str
    operation_name: str
    
    # Full qualified name for search
    name: str  # "{service}.{operation}"
    description: Optional[str] = None
    
    # SOAP specifics
    soap_action: Optional[str] = None
    style: SOAPStyle = SOAPStyle.DOCUMENT
    namespace: str
    
    # Schema information (JSON Schema format)
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    fault_schema: Optional[Dict[str, Any]] = None
    
    # For BM25/hybrid search
    search_content: str = ""
    
    # Protocol details (stored as JSONB)
    protocol_details: Dict[str, Any] = Field(default_factory=dict)
    
    # Metadata
    is_enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    def to_protocol_details(self) -> Dict[str, Any]:
        """Convert to protocol_details JSONB format"""
        return {
            "protocol": "soap",
            "wsdl_url": self.protocol_details.get("wsdl_url"),
            "service": self.service_name,
            "port": self.port_name,
            "operation": self.operation_name,
            "soap_action": self.soap_action,
            "style": self.style.value,
            "namespace": self.namespace,
        }


class SOAPConnectorConfig(BaseModel):
    """Configuration for a SOAP connector"""
    
    # WSDL source
    wsdl_url: str  # Can be URL or file path
    
    # Authentication
    auth_type: SOAPAuthType = SOAPAuthType.NONE
    
    # Basic auth
    username: Optional[str] = None
    password: Optional[str] = None
    
    # WS-Security
    ws_security_username: Optional[str] = None
    ws_security_password: Optional[str] = None
    ws_security_use_digest: bool = False  # Use password digest instead of plain text
    ws_security_use_timestamp: bool = True  # Add Timestamp element (recommended)
    ws_security_timestamp_ttl: int = 300  # Timestamp validity in seconds (5 min default)
    ws_security_use_nonce: bool = True  # Add Nonce to prevent replay attacks
    
    # Certificate auth
    client_cert_path: Optional[str] = None
    client_key_path: Optional[str] = None
    ca_cert_path: Optional[str] = None
    
    # Session-based auth (VMware style)
    login_operation: Optional[str] = None  # e.g., "SessionManager.Login"
    logout_operation: Optional[str] = None  # e.g., "SessionManager.Logout"
    session_cookie_name: Optional[str] = None  # Cookie name to track session
    
    # Connection settings
    timeout: int = 30  # seconds
    verify_ssl: bool = True
    endpoint_override: Optional[str] = None  # Override endpoint from WSDL (e.g., actual host)
    
    # Caching
    cache_wsdl: bool = True  # Cache parsed WSDL
    wsdl_cache_ttl: int = 3600  # 1 hour
    
    def get_endpoint_url(self) -> Optional[str]:
        """Get endpoint URL, deriving from WSDL URL if not explicitly set.
        
        Many WSDLs (like vCenter's) define localhost as the endpoint, but
        the actual endpoint should be derived from the WSDL URL.
        """
        if self.endpoint_override:
            return self.endpoint_override
        
        # Derive from WSDL URL: https://host/sdk/vimService.wsdl -> https://host/sdk
        if self.wsdl_url and self.wsdl_url.startswith(('http://', 'https://')):
            from urllib.parse import urlparse, urlunparse
            parsed = urlparse(self.wsdl_url)
            # Remove the filename (e.g., vimService.wsdl)
            path = parsed.path.rsplit('/', 1)[0] if '/' in parsed.path else parsed.path
            return urlunparse((parsed.scheme, parsed.netloc, path, '', '', ''))
        
        return None


class SOAPCallParams(BaseModel):
    """Parameters for calling a SOAP operation"""
    
    operation_name: str
    params: Dict[str, Any] = Field(default_factory=dict)
    
    # Optional overrides
    service_name: Optional[str] = None
    port_name: Optional[str] = None
    timeout: Optional[int] = None


class SOAPResponse(BaseModel):
    """Response from a SOAP operation"""
    
    success: bool
    status_code: int = 200  # HTTP status (200 for success, 500 for SOAP fault)
    body: Dict[str, Any] = Field(default_factory=dict)
    headers: Dict[str, str] = Field(default_factory=dict)
    
    # Error information
    fault_code: Optional[str] = None
    fault_string: Optional[str] = None
    fault_detail: Optional[str] = None
    
    # Metadata
    operation_name: Optional[str] = None
    duration_ms: Optional[float] = None


# =============================================================================
# TASK-96: SOAP Type Definitions for Schema Ingestion
# =============================================================================


class SOAPProperty(BaseModel):
    """A property/field on a SOAP complex type.
    
    Represents an element or attribute from a WSDL complexType definition.
    This enables the agent to discover what properties exist on types.
    """
    
    name: str  # Property name (e.g., "recommendation", "host")
    type_name: str  # Type name (e.g., "ClusterRecommendation", "string")
    is_array: bool = False  # True if maxOccurs > 1
    is_required: bool = False  # True if minOccurs > 0
    description: Optional[str] = None  # From XML documentation/annotation


class SOAPTypeDefinition(BaseModel):
    """A complex type definition from WSDL schema.
    
    This model represents a complexType extracted from WSDL, enabling
    the agent to discover:
    - What types exist in the service
    - What properties/fields each type has
    - Type inheritance relationships
    
    These are indexed as knowledge chunks for BM25/hybrid search.
    """
    
    name: str  # Type name (e.g., "ClusterComputeResource")
    namespace: str  # XML namespace (e.g., "urn:vim25")
    base_type: Optional[str] = None  # Parent type if this extends another
    properties: List[SOAPProperty] = Field(default_factory=list)
    description: Optional[str] = None  # From XML documentation/annotation
    
    # For tracking which connector/tenant owns this type
    connector_id: Optional[UUID] = None
    tenant_id: Optional[str] = None
    
    @property
    def search_content(self) -> str:
        """Generate searchable text for BM25 indexing.
        
        Includes type name, base type, and all property names to
        enable discovery via search queries like "cluster recommendation".
        """
        parts = [self.name]
        
        if self.base_type:
            parts.append(f"extends {self.base_type}")
        
        if self.description:
            parts.append(self.description)
        
        # Include all property names for search
        prop_names = [p.name for p in self.properties]
        if prop_names:
            parts.append(f"properties: {' '.join(prop_names)}")
        
        return " ".join(parts)
    
    def to_knowledge_content(self) -> str:
        """Generate human-readable content for knowledge chunks.
        
        This is what the agent sees when searching for types.
        """
        lines = [f"SOAP Type: {self.name}"]
        lines.append(f"Namespace: {self.namespace}")
        
        if self.base_type:
            lines.append(f"Extends: {self.base_type}")
        
        if self.description:
            lines.append(f"Description: {self.description}")
        
        if self.properties:
            lines.append("\nProperties:")
            for prop in self.properties:
                array_marker = "[]" if prop.is_array else ""
                required_marker = " (required)" if prop.is_required else ""
                lines.append(f"  - {prop.name}: {prop.type_name}{array_marker}{required_marker}")
        
        return "\n".join(lines)

