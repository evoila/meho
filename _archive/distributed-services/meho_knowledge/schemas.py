"""
Pydantic schemas for knowledge service.
"""
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime
from enum import Enum


class ContentType(str, Enum):
    """Type of content in a chunk"""
    DESCRIPTION = "description"          # Explanatory text
    EXAMPLE_JSON = "example_json"        # JSON response example
    EXAMPLE_CODE = "example_code"        # Code snippet
    SCHEMA = "schema"                    # API schema definition
    PARAMETERS = "parameters"            # Parameter descriptions
    OVERVIEW = "overview"                # Chapter/section overview
    TABLE = "table"                      # Tabular data
    LIST = "list"                        # Bulleted/numbered lists


class ChunkMetadata(BaseModel):
    """Rich metadata for knowledge chunks"""
    
    # Document structure
    chapter: Optional[str] = Field(default=None, description="Top-level chapter name")
    section: Optional[str] = Field(default=None, description="Section name")
    subsection: Optional[str] = Field(default=None, description="Subsection name")
    heading_hierarchy: List[str] = Field(default_factory=list, description="Full heading hierarchy")
    
    # API-specific metadata
    endpoint_path: Optional[str] = Field(default=None, description="API endpoint path (e.g., /v1/roles)")
    http_method: Optional[str] = Field(default=None, description="HTTP method (GET, POST, etc.)")
    resource_type: Optional[str] = Field(default=None, description="Resource type (roles, users, etc.)")
    
    # Content classification
    content_type: ContentType = Field(default=ContentType.DESCRIPTION, description="Type of content")
    has_code_example: bool = Field(default=False, description="Contains code example")
    has_json_example: bool = Field(default=False, description="Contains JSON example")
    has_table: bool = Field(default=False, description="Contains table")
    
    # Searchability boosters
    keywords: List[str] = Field(default_factory=list, description="Important terms extracted from text")
    entity_names: List[str] = Field(default_factory=list, description="Named entities")
    
    # Technical identifiers
    programming_language: Optional[str] = Field(default=None, description="Programming language of code")
    response_codes: List[int] = Field(default_factory=list, description="HTTP response codes mentioned")
    
    model_config = ConfigDict(extra="allow", use_enum_values=True)


class KnowledgeType(str, Enum):
    """
    Type of knowledge chunk.
    
    Different types have different lifecycle management:
    - DOCUMENTATION: Permanent reference material (PDFs, architecture docs)
    - PROCEDURE: Permanent operational procedures (runbooks, guides)
    - EVENT: Temporary webhook events (expires after N days)
    - EVENT_SUMMARY: Aggregated event summaries (longer retention)
    - TREND: Trend analysis (aggregated over time)
    """
    DOCUMENTATION = "documentation"
    PROCEDURE = "procedure"
    EVENT = "event"
    EVENT_SUMMARY = "event_summary"
    TREND = "trend"


class KnowledgeChunkCreate(BaseModel):
    """Schema for creating a knowledge chunk (no ID)"""
    
    text: str = Field(..., min_length=1, max_length=100000, description="Knowledge text content")
    tenant_id: Optional[str] = Field(default=None, description="Tenant ID (null for global)")
    system_id: Optional[str] = Field(default=None, description="System ID (null for tenant-wide)")
    user_id: Optional[str] = Field(default=None, description="User ID (null for non-user-specific)")
    roles: List[str] = Field(default_factory=list, description="Required roles to access")
    groups: List[str] = Field(default_factory=list, description="Required groups to access")
    tags: List[str] = Field(default_factory=list, description="Tags for categorization")
    source_uri: Optional[str] = Field(default=None, description="Source document URI")
    
    # Lifecycle management fields
    expires_at: Optional[datetime] = Field(default=None, description="Auto-delete after this time (null = never expires)")
    knowledge_type: KnowledgeType = Field(default=KnowledgeType.DOCUMENTATION, description="Type of knowledge (determines lifecycle)")
    priority: int = Field(default=0, ge=-100, le=100, description="Search ranking priority (higher = more important)")
    
    # Rich metadata for enhanced retrieval  
    search_metadata: Optional[ChunkMetadata] = Field(default=None, description="Structured metadata for enhanced search")


class KnowledgeChunk(KnowledgeChunkCreate):
    """Schema for a complete knowledge chunk (with ID and timestamps)"""
    
    id: str
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class KnowledgeChunkFilter(BaseModel):
    """Schema for filtering knowledge chunks"""
    
    tenant_id: Optional[str] = Field(default=None, description="Filter by tenant")
    system_id: Optional[str] = Field(default=None, description="Filter by system")
    user_id: Optional[str] = Field(default=None, description="Filter by user")
    tags: Optional[List[str]] = Field(default=None, description="Filter by tags (AND logic)")
    knowledge_type: Optional[KnowledgeType] = Field(default=None, description="Filter by knowledge type")
    created_after: Optional[datetime] = Field(default=None, description="Filter by creation date (only chunks created after this)")
    source_uri: Optional[str] = Field(default=None, description="Filter by source URI (e.g., job:123 for document deletion)")
    source_uri_prefix: Optional[str] = Field(default=None, description="Filter by source URI prefix (e.g., soap://connector-id/ for SOAP cleanup)")
    limit: int = Field(default=100, ge=1, le=1000, description="Max results")
    offset: int = Field(default=0, ge=0, description="Pagination offset")

