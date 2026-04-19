# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Pydantic schemas for SOAP connector type.

Contains schemas for:
- SoapOperationDescriptor: SOAP operations from WSDL
- SoapTypeDescriptor: SOAP complex types from WSDL schema
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ============================================================================
# SOAP Operation Schemas
# ============================================================================


class SoapOperationDescriptorCreate(BaseModel):
    """Create a SOAP operation descriptor"""

    connector_id: str
    tenant_id: str
    service_name: str
    port_name: str
    operation_name: str
    name: str  # Full name: "ServiceName.OperationName"
    description: str | None = None
    soap_action: str | None = None
    namespace: str | None = None
    style: str = "document"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    protocol_details: dict[str, Any] = Field(default_factory=dict)
    search_content: str | None = None
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

    service_name: str | None = None
    search: str | None = None
    is_enabled: bool | None = None
    safety_level: Literal["safe", "caution", "dangerous"] | None = None


# ============================================================================
# SOAP Type Schemas
# ============================================================================


class SoapPropertySchema(BaseModel):
    """A property on a SOAP type"""

    name: str
    type_name: str
    is_array: bool = False
    is_required: bool = False
    description: str | None = None


class SoapTypeDescriptorCreate(BaseModel):
    """Create a SOAP type descriptor"""

    connector_id: str
    tenant_id: str
    type_name: str
    namespace: str | None = None
    base_type: str | None = None
    properties: list[SoapPropertySchema] = Field(default_factory=list)
    description: str | None = None
    search_content: str | None = None

    @field_validator("properties", mode="before")
    @classmethod
    def validate_properties(cls, v: Any) -> Any:
        """Ensure properties are properly formatted (accepts dicts or SoapPropertySchema)"""
        if isinstance(v, list):
            return [SoapPropertySchema(**p) if isinstance(p, dict) else p for p in v]
        return v


class SoapTypeDescriptor(SoapTypeDescriptorCreate):
    """SOAP type descriptor with ID and timestamps"""

    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SoapTypeFilter(BaseModel):
    """Filter for searching SOAP types"""

    search: str | None = None
    base_type: str | None = None


__all__ = [
    "SoapOperationDescriptor",
    "SoapOperationDescriptorCreate",
    "SoapOperationFilter",
    "SoapPropertySchema",
    "SoapTypeDescriptor",
    "SoapTypeDescriptorCreate",
    "SoapTypeFilter",
]
