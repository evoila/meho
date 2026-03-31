"""
SQLAlchemy models for OpenAPI service.
"""
from sqlalchemy import Column, String, Text, TIMESTAMP, ForeignKey, Index, Boolean, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime
import uuid
import enum

Base = declarative_base()


class ProtocolType(enum.Enum):
    """Supported API protocols"""
    REST = "rest"       # OpenAPI/REST APIs
    GRAPHQL = "graphql" # GraphQL APIs
    GRPC = "grpc"       # gRPC services
    SOAP = "soap"       # SOAP/WSDL services


class ConnectorType(enum.Enum):
    """
    Connector implementation types (TASK-97).
    
    Determines which connector implementation is used:
    - REST: OpenAPI/REST via HTTP client
    - SOAP: WSDL/SOAP via zeep
    - VMWARE: vSphere via pyvmomi
    """
    REST = "rest"       # REST/OpenAPI - uses HTTP client
    SOAP = "soap"       # SOAP/WSDL - uses zeep client
    VMWARE = "vmware"   # VMware vSphere - uses pyvmomi


class ConnectorModel(Base):  # type: ignore[misc,valid-type]
    """API connector configuration"""
    
    __tablename__ = "connector"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    base_url = Column(String, nullable=False)
    
    # Connector type - single source of truth for connector classification
    # Values: rest, soap, graphql, grpc, vmware, kubernetes
    connector_type = Column(String, nullable=False, default="rest")
    
    # Protocol-specific configuration (varies by connector_type)
    # REST: { "openapi_url": "..." }
    # GraphQL: { "endpoint_url": "...", "introspection_enabled": true }
    # gRPC: { "server_address": "...", "proto_source": "file|reflection" }
    # SOAP: { "wsdl_url": "...", "auth_type": "basic|session|ws_security" }
    # VMware: { "vcenter_host": "...", "port": 443, "disable_ssl_verification": false }
    protocol_config = Column(JSONB, nullable=True)
    
    # Authentication
    auth_type = Column(String, nullable=False)  # API_KEY, OAUTH2, BASIC, NONE, SESSION
    auth_config = Column(JSONB, nullable=False, default=dict)  # Encrypted auth details
    credential_strategy = Column(String, nullable=False, default="SYSTEM")  # SYSTEM or USER_PROVIDED
    
    # Session-based authentication configuration
    login_url = Column(String, nullable=True)  # Login endpoint path (e.g., /api/v1/auth/login)
    login_method = Column(String, nullable=True)  # HTTP method for login (POST, GET)
    login_config = Column(JSONB, nullable=True)  # Login request/response configuration
    
    # Safety Policies (Task 22)
    allowed_methods = Column(JSONB, nullable=False, default=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE"])
    blocked_methods = Column(JSONB, nullable=False, default=list)
    default_safety_level = Column(String, nullable=False, default="safe")
    
    # Metadata
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    specs = relationship("OpenAPISpecModel", back_populates="connector", cascade="all, delete-orphan")
    endpoints = relationship("EndpointDescriptorModel", back_populates="connector", cascade="all, delete-orphan")
    user_credentials = relationship("UserConnectorCredentialModel", back_populates="connector", cascade="all, delete-orphan")
    soap_operations = relationship("SoapOperationDescriptorModel", back_populates="connector", cascade="all, delete-orphan")
    soap_types = relationship("SoapTypeDescriptorModel", back_populates="connector", cascade="all, delete-orphan")
    typed_operations = relationship("ConnectorOperationModel", back_populates="connector", cascade="all, delete-orphan")
    typed_types = relationship("ConnectorTypeModel", back_populates="connector", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index('ix_connector_tenant_name', 'tenant_id', 'name'),
    )


class OpenAPISpecModel(Base):  # type: ignore[misc,valid-type]
    """OpenAPI specification metadata"""
    
    __tablename__ = "openapi_spec"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    
    storage_uri = Column(Text, nullable=False)  # S3 path to spec file
    version = Column(String, nullable=True)  # OpenAPI version (3.0, 3.1)
    spec_version = Column(String, nullable=True)  # API version from info.version
    
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    
    connector = relationship("ConnectorModel", back_populates="specs")


class EndpointDescriptorModel(Base):  # type: ignore[misc,valid-type]
    """API endpoint descriptor from OpenAPI spec"""
    
    __tablename__ = "endpoint_descriptor"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    
    # Endpoint details
    method = Column(String, nullable=False)  # GET, POST, PUT, DELETE, etc.
    path = Column(String, nullable=False)  # /customers/{id}
    operation_id = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    
    # Metadata for discovery
    tags = Column(JSONB, nullable=False, default=list)
    required_params = Column(JSONB, nullable=False, default=list)
    
    # Full schema details (for validation and execution)
    path_params_schema = Column(JSONB, nullable=False, default=dict)
    query_params_schema = Column(JSONB, nullable=False, default=dict)
    body_schema = Column(JSONB, nullable=False, default=dict)
    response_schema = Column(JSONB, nullable=False, default=dict)
    
    # Session 78: Explicit parameter metadata for LLM guidance
    parameter_metadata = Column(JSONB, nullable=True)
    
    # TASK-81: LLM instructions for schema-guided parameter collection
    # Stores conversation strategy, parameter handling rules, example conversations
    llm_instructions = Column(JSONB, nullable=True)
    
    # Activation & Safety (Task 22)
    is_enabled = Column(Boolean, nullable=False, default=True)
    safety_level = Column(String, nullable=False, default="safe")  # safe, caution, dangerous
    requires_approval = Column(Boolean, nullable=False, default=False)
    
    # Enhanced Documentation (Task 22)
    custom_description = Column(Text, nullable=True)  # Admin-written description
    custom_notes = Column(Text, nullable=True)  # Internal admin notes
    usage_examples = Column(JSONB, nullable=True)  # Example payloads
    
    # Agent Learning (future use)
    agent_notes = Column(Text, nullable=True)
    common_errors = Column(JSONB, nullable=True)
    success_patterns = Column(JSONB, nullable=True)
    
    # Audit Trail
    last_modified_by = Column(String, nullable=True)
    last_modified_at = Column(TIMESTAMP, nullable=True)
    
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    
    connector = relationship("ConnectorModel", back_populates="endpoints")
    
    __table_args__ = (
        Index('ix_endpoint_connector_method_path', 'connector_id', 'method', 'path'),
        Index('ix_endpoint_connector_tags', 'connector_id'),
        Index('ix_endpoint_enabled', 'connector_id', 'is_enabled'),
        Index('ix_endpoint_safety', 'connector_id', 'safety_level'),
    )


class UserConnectorCredentialModel(Base):  # type: ignore[misc,valid-type]
    """User-specific credentials for RBAC-enabled connectors"""
    
    __tablename__ = "user_connector_credential"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    
    # Encrypted credentials
    credential_type = Column(String, nullable=False)  # PASSWORD, API_KEY, OAUTH2_TOKEN, SESSION
    encrypted_credentials = Column(Text, nullable=False)
    
    # OAuth2 specific
    oauth2_refresh_token = Column(Text, nullable=True)
    oauth2_token_expires_at = Column(TIMESTAMP, nullable=True)
    
    # Session-based auth state (for SESSION auth type)
    session_token = Column(Text, nullable=True)  # Current session token (encrypted)
    session_token_expires_at = Column(TIMESTAMP, nullable=True)  # When session expires
    session_refresh_token = Column(Text, nullable=True)  # Refresh token (encrypted)
    session_refresh_expires_at = Column(TIMESTAMP, nullable=True)  # Refresh token expiry
    session_state = Column(String, nullable=True)  # LOGGED_OUT, LOGGED_IN, EXPIRED
    
    # Metadata
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_used_at = Column(TIMESTAMP, nullable=True)
    
    connector = relationship("ConnectorModel", back_populates="user_credentials")
    
    __table_args__ = (
        Index('ix_user_connector', 'user_id', 'connector_id', unique=True),
    )


class SoapOperationDescriptorModel(Base):  # type: ignore[misc,valid-type]
    """
    SOAP operation descriptor from WSDL.
    
    Mirrors EndpointDescriptorModel pattern for REST endpoints.
    Stored in DB for:
    - Fast retrieval without re-parsing WSDL
    - BM25 search on-the-fly
    - Frontend display in WSDL tab
    """
    
    __tablename__ = "soap_operation_descriptor"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)
    
    # SOAP operation identification
    service_name = Column(String, nullable=False)  # e.g., "VimService"
    port_name = Column(String, nullable=False)     # e.g., "VimPort"
    operation_name = Column(String, nullable=False) # e.g., "RetrieveProperties"
    
    # Full operation name for display: "VimService.RetrieveProperties"
    name = Column(String, nullable=False)
    
    # Documentation
    description = Column(Text, nullable=True)
    
    # SOAP-specific details
    soap_action = Column(String, nullable=True)  # e.g., "urn:vim25/8.0.3.0"
    namespace = Column(String, nullable=True)    # Target namespace
    style = Column(String, nullable=False, default="document")  # document/rpc
    
    # Full schema details (JSONB for complex nested structures)
    input_schema = Column(JSONB, nullable=False, default=dict)
    output_schema = Column(JSONB, nullable=False, default=dict)
    
    # Protocol details for execution
    protocol_details = Column(JSONB, nullable=False, default=dict)
    
    # Search optimization: pre-computed search content for BM25
    search_content = Column(Text, nullable=True)
    
    # Activation & Safety (like REST endpoints)
    is_enabled = Column(Boolean, nullable=False, default=True)
    safety_level = Column(String, nullable=False, default="caution")  # SOAP defaults to caution
    requires_approval = Column(Boolean, nullable=False, default=False)
    
    # Timestamps
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    connector = relationship("ConnectorModel", back_populates="soap_operations")
    
    __table_args__ = (
        Index('ix_soap_op_connector', 'connector_id'),
        Index('ix_soap_op_connector_service', 'connector_id', 'service_name'),
        Index('ix_soap_op_connector_operation', 'connector_id', 'operation_name'),
        Index('ix_soap_op_tenant', 'tenant_id'),
    )


class SoapTypeDescriptorModel(Base):  # type: ignore[misc,valid-type]
    """
    SOAP type definition from WSDL schema.
    
    Stores complexType definitions for:
    - Agent discovery of data types
    - Frontend display in WSDL tab
    - BM25 search on-the-fly
    """
    
    __tablename__ = "soap_type_descriptor"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)
    
    # Type identification
    type_name = Column(String, nullable=False)  # e.g., "ClusterComputeResource"
    namespace = Column(String, nullable=True)   # e.g., "urn:vim25"
    
    # Type inheritance
    base_type = Column(String, nullable=True)  # Parent type if extends
    
    # Properties as JSONB array: [{name, type_name, is_array, is_required, description}]
    properties = Column(JSONB, nullable=False, default=list)
    
    # Documentation
    description = Column(Text, nullable=True)
    
    # Search optimization: pre-computed search content for BM25
    # Includes type_name, base_type, all property names
    search_content = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    connector = relationship("ConnectorModel", back_populates="soap_types")
    
    __table_args__ = (
        Index('ix_soap_type_connector', 'connector_id'),
        Index('ix_soap_type_connector_name', 'connector_id', 'type_name'),
        Index('ix_soap_type_tenant', 'tenant_id'),
    )


class ConnectorOperationModel(Base):  # type: ignore[misc,valid-type]
    """
    Generic operations table for typed connectors (TASK-97).
    
    Used by VMware, Kubernetes, and other typed connectors.
    REST uses endpoint_descriptor table.
    SOAP uses soap_operation_descriptor table.
    
    This allows the agent to discover operations uniformly via
    search_operations regardless of connector type.
    """
    
    __tablename__ = "connector_operation"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)
    
    # Operation identification
    operation_id = Column(String, nullable=False)  # "list_virtual_machines"
    name = Column(String, nullable=False)          # "List Virtual Machines"
    description = Column(Text, nullable=True)
    category = Column(String, nullable=True)       # "compute", "storage", "networking"
    
    # Parameters as JSONB array: [{name, type, required, description}]
    parameters = Column(JSONB, nullable=False, default=list)
    
    # Example usage
    example = Column(String, nullable=True)
    
    # Search optimization: pre-computed search content for BM25
    # Includes name, description, category, parameter names
    search_content = Column(Text, nullable=True)
    
    # Activation & Safety
    is_enabled = Column(Boolean, nullable=False, default=True)
    safety_level = Column(String, nullable=False, default="safe")  # safe, caution, dangerous
    requires_approval = Column(Boolean, nullable=False, default=False)
    
    # Timestamps
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    connector = relationship("ConnectorModel", back_populates="typed_operations")
    
    __table_args__ = (
        Index('ix_conn_op_connector', 'connector_id'),
        Index('ix_conn_op_connector_operation', 'connector_id', 'operation_id'),
        Index('ix_conn_op_tenant', 'tenant_id'),
        Index('ix_conn_op_category', 'connector_id', 'category'),
    )


class ConnectorTypeModel(Base):  # type: ignore[misc,valid-type]
    """
    Generic types table for typed connectors (TASK-97).
    
    Used by VMware, Kubernetes, and other typed connectors.
    Stores entity type definitions for agent discovery.
    
    This allows the agent to understand what entities exist
    (VirtualMachine, Cluster, Pod, etc.) regardless of connector type.
    """
    
    __tablename__ = "connector_type"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey('connector.id'), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)
    
    # Type identification
    type_name = Column(String, nullable=False)  # "VirtualMachine"
    description = Column(Text, nullable=True)
    category = Column(String, nullable=True)    # "compute", "storage", "networking"
    
    # Properties as JSONB array: [{name, type, description}]
    properties = Column(JSONB, nullable=False, default=list)
    
    # Search optimization: pre-computed search content for BM25
    search_content = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    connector = relationship("ConnectorModel", back_populates="typed_types")
    
    __table_args__ = (
        Index('ix_conn_type_connector', 'connector_id'),
        Index('ix_conn_type_connector_name', 'connector_id', 'type_name'),
        Index('ix_conn_type_tenant', 'tenant_id'),
    )

