# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SQLAlchemy models for SOAP connector type.

Contains database models for:
- SoapOperationDescriptorModel: SOAP operation descriptors from WSDL
- SoapTypeDescriptorModel: SOAP type definitions from WSDL schema

These are the persistence layer counterparts to the Pydantic models in models.py.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import TIMESTAMP, Boolean, Column, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from meho_app.database import Base


class SoapOperationDescriptorModel(Base):
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
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)

    # SOAP operation identification
    service_name = Column(String, nullable=False)  # e.g., "VimService"
    port_name = Column(String, nullable=False)  # e.g., "VimPort"
    operation_name = Column(String, nullable=False)  # e.g., "RetrieveProperties"

    # Full operation name for display: "VimService.RetrieveProperties"
    name = Column(String, nullable=False)

    # Documentation
    description = Column(Text, nullable=True)

    # SOAP-specific details
    soap_action = Column(String, nullable=True)  # e.g., "urn:vim25/8.0.3.0"
    namespace = Column(String, nullable=True)  # Target namespace
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
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    connector = relationship("ConnectorModel", back_populates="soap_operations")

    __table_args__ = (
        Index("ix_soap_op_connector", "connector_id"),
        Index("ix_soap_op_connector_service", "connector_id", "service_name"),
        Index("ix_soap_op_connector_operation", "connector_id", "operation_name"),
        Index("ix_soap_op_tenant", "tenant_id"),
    )


class SoapTypeDescriptorModel(Base):
    """
    SOAP type definition from WSDL schema.

    Stores complexType definitions for:
    - Agent discovery of data types
    - Frontend display in WSDL tab
    - BM25 search on-the-fly
    """

    __tablename__ = "soap_type_descriptor"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connector_id = Column(UUID(as_uuid=True), ForeignKey("connector.id"), nullable=False)
    tenant_id = Column(String, nullable=False, index=True)

    # Type identification
    type_name = Column(String, nullable=False)  # e.g., "ClusterComputeResource"
    namespace = Column(String, nullable=True)  # e.g., "urn:vim25"

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
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    connector = relationship("ConnectorModel", back_populates="soap_types")

    __table_args__ = (
        Index("ix_soap_type_connector", "connector_id"),
        Index("ix_soap_type_connector_name", "connector_id", "type_name"),
        Index("ix_soap_type_tenant", "tenant_id"),
    )


__all__ = [
    "SoapOperationDescriptorModel",
    "SoapTypeDescriptorModel",
]
