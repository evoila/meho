# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for event templates.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EventTemplateCreate(BaseModel):
    """Schema for creating an event template"""

    connector_id: str = Field(..., description="Connector ID (must exist)")
    event_type: str = Field(..., description="Event type (e.g., 'push', 'alert')")
    text_template: str = Field(..., description="Jinja2 template for text extraction")
    tag_rules: list[str] = Field(default_factory=list, description="Jinja2 expressions for tags")
    issue_detection_rule: str | None = Field(
        None, description="Jinja2 boolean expression for issue detection"
    )
    tenant_id: str = Field(..., description="Tenant ID")


class EventTemplateUpdate(BaseModel):
    """Schema for updating an event template"""

    text_template: str | None = Field(None, description="Jinja2 template for text extraction")
    tag_rules: list[str] | None = Field(None, description="Jinja2 expressions for tags")
    issue_detection_rule: str | None = Field(
        None, description="Jinja2 boolean expression for issue detection"
    )


class EventTemplate(BaseModel):
    """Schema for a complete event template (with ID and timestamps)"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    connector_id: str
    event_type: str
    text_template: str
    tag_rules: list[str]
    issue_detection_rule: str | None
    tenant_id: str
    created_at: datetime
    updated_at: datetime


class EventTemplateFilter(BaseModel):
    """Schema for filtering event templates"""

    connector_id: str | None = Field(None, description="Filter by connector")
    event_type: str | None = Field(None, description="Filter by event type")
    tenant_id: str | None = Field(None, description="Filter by tenant")
    limit: int = Field(default=100, ge=1, le=1000, description="Max results")
    offset: int = Field(default=0, ge=0, description="Pagination offset")
