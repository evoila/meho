"""
Pydantic schemas for event templates.
"""
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, List
from datetime import datetime


class EventTemplateCreate(BaseModel):
    """Schema for creating an event template"""
    
    connector_id: str = Field(..., description="Connector ID (must exist)")
    event_type: str = Field(..., description="Event type (e.g., 'push', 'alert')")
    text_template: str = Field(..., description="Jinja2 template for text extraction")
    tag_rules: List[str] = Field(default_factory=list, description="Jinja2 expressions for tags")
    issue_detection_rule: Optional[str] = Field(None, description="Jinja2 boolean expression for issue detection")
    tenant_id: str = Field(..., description="Tenant ID")


class EventTemplateUpdate(BaseModel):
    """Schema for updating an event template"""
    
    text_template: Optional[str] = Field(None, description="Jinja2 template for text extraction")
    tag_rules: Optional[List[str]] = Field(None, description="Jinja2 expressions for tags")
    issue_detection_rule: Optional[str] = Field(None, description="Jinja2 boolean expression for issue detection")


class EventTemplate(BaseModel):
    """Schema for a complete event template (with ID and timestamps)"""
    model_config = ConfigDict(from_attributes=True)
    
    id: str
    connector_id: str
    event_type: str
    text_template: str
    tag_rules: List[str]
    issue_detection_rule: Optional[str]
    tenant_id: str
    created_at: datetime
    updated_at: datetime


class EventTemplateFilter(BaseModel):
    """Schema for filtering event templates"""
    
    connector_id: Optional[str] = Field(None, description="Filter by connector")
    event_type: Optional[str] = Field(None, description="Filter by event type")
    tenant_id: Optional[str] = Field(None, description="Filter by tenant")
    limit: int = Field(default=100, ge=1, le=1000, description="Max results")
    offset: int = Field(default=0, ge=0, description="Pagination offset")

