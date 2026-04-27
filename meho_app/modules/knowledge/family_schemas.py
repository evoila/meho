# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Pydantic schemas for document families."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class DocumentFamilyCreate(BaseModel):
    """Schema for creating a new document family."""

    tenant_id: str = Field(..., description="Tenant ID")
    name: str = Field(..., min_length=1, max_length=512, description="Display name (filename stem)")
    scope_type: str = Field(..., description="Scope: 'global', 'type', or 'instance'")
    connector_id: str | None = Field(
        None, description="Connector instance ID (required for instance scope)"
    )
    connector_type_scope: str | None = Field(
        None, description="Connector type scope (required for type scope)"
    )
    knowledge_type: str = Field(
        default="documentation", description="Knowledge type (inherited by versions)"
    )
    tags: list[str] = Field(default_factory=list, description="Tags inherited by versions")
    created_by_user_id: str | None = Field(None, description="User who created the family")


class DocumentFamily(BaseModel):
    """Complete document family record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    name: str
    scope_type: str
    connector_id: UUID | None = None
    connector_type_scope: str | None = None
    knowledge_type: str
    tags: list[str] = Field(default_factory=list)
    created_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("id")
    def serialize_id(self, value: UUID) -> str:
        return str(value)

    @field_serializer("connector_id")
    def serialize_connector_id(self, value: UUID | None) -> str | None:
        return str(value) if value else None


class DocumentVersion(BaseModel):
    """A single version within a document family (backed by an IngestionJob)."""

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    doc_version: str
    filename: str | None
    file_size: int | None
    file_hash: str | None
    status: str
    chunks_created: int
    created_at: datetime

    @field_serializer("job_id")
    def serialize_job_id(self, value: UUID) -> str:
        return str(value)
