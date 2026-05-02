# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for REST/OpenAPI connector type.

Contains:
- OpenAPISpecModel: Metadata about ingested OpenAPI specifications
- EndpointDescriptorModel: Individual API endpoint descriptors
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, Boolean, Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class OpenAPISpecModel(Base):
    """OpenAPI specification metadata"""

    __tablename__ = "openapi_spec"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)

    storage_uri = Column(Text, nullable=False)  # S3 path to spec file
    version = Column(String, nullable=True)  # OpenAPI version (3.0, 3.1)
    spec_version = Column(String, nullable=True)  # API version from info.version

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    connector = relationship("ConnectorModel", back_populates="specs")


class EndpointDescriptorModel(Base):
    """API endpoint descriptor from OpenAPI spec"""

    __tablename__ = "endpoint_descriptor"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)

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
    last_modified_at = Column(TIMESTAMP(timezone=True), nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    connector = relationship("ConnectorModel", back_populates="endpoints")

    __table_args__ = (
        Index("ix_endpoint_connector_method_path", "connector_id", "method", "path"),
        Index("ix_endpoint_connector_tags", "connector_id"),
        Index("ix_endpoint_enabled", "connector_id", "is_enabled"),
        Index("ix_endpoint_safety", "connector_id", "safety_level"),
    )


__all__ = [
    "EndpointDescriptorModel",
    "OpenAPISpecModel",
]
