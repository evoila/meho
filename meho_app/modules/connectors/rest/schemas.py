# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for REST/OpenAPI connector type.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# OpenAPI Spec Schemas
# ============================================================================


class OpenAPISpecCreate(BaseModel):
    """Create OpenAPI spec record."""

    connector_id: str
    storage_uri: str
    version: str | None = None
    spec_version: str | None = None


class OpenAPISpec(OpenAPISpecCreate):
    """OpenAPI spec with ID."""

    id: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ============================================================================
# Endpoint Descriptor Schemas
# ============================================================================


class EndpointDescriptorCreate(BaseModel):
    """Create endpoint descriptor."""

    connector_id: str
    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    required_params: list[str] = Field(default_factory=list)
    path_params_schema: dict[str, Any] = Field(default_factory=dict)
    query_params_schema: dict[str, Any] = Field(default_factory=dict)
    body_schema: dict[str, Any] = Field(default_factory=dict)
    response_schema: dict[str, Any] = Field(default_factory=dict)

    # Explicit parameter metadata for LLM guidance
    parameter_metadata: dict[str, Any] | None = Field(
        default=None, description="Explicit parameter requirements for LLM workflow building"
    )

    # LLM instructions for schema-guided parameter collection
    llm_instructions: dict[str, Any] | None = Field(
        default=None,
        description="LLM guidance for helping users through complex parameter collection",
    )

    # Activation & Safety
    is_enabled: bool = True
    safety_level: Literal["safe", "caution", "dangerous"] = "safe"
    requires_approval: bool = False

    # Enhanced Documentation
    custom_description: str | None = None
    custom_notes: str | None = None
    usage_examples: dict[str, Any] | None = None


class EndpointDescriptor(EndpointDescriptorCreate):
    """Endpoint descriptor with ID."""

    id: str
    created_at: datetime

    # Audit Trail
    last_modified_by: str | None = None
    last_modified_at: datetime | None = None

    # Agent Learning (future)
    agent_notes: str | None = None
    common_errors: list[dict[str, Any]] | None = None
    success_patterns: list[dict[str, Any]] | None = None

    model_config = ConfigDict(from_attributes=True)


class EndpointUpdate(BaseModel):
    """Update endpoint configuration."""

    is_enabled: bool | None = None
    safety_level: (
        Literal["safe", "caution", "dangerous", "read", "write", "destructive", "auto"] | None
    ) = None
    requires_approval: bool | None = None
    custom_description: str | None = None
    custom_notes: str | None = None
    usage_examples: dict[str, Any] | None = None
    llm_instructions: dict[str, Any] | None = None


class EndpointFilter(BaseModel):
    """Filter for searching endpoints."""

    connector_id: str | None = None
    method: str | None = None
    tags: list[str] | None = None
    search_text: str | None = None
    is_enabled: bool | None = None
    safety_level: Literal["safe", "caution", "dangerous"] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
