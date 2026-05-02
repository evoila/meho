# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
SOAP/WSDL Support for MEHO

This module provides generic SOAP support via the zeep library,
enabling MEHO to integrate with any WSDL-based API including:
- VMware VIM API (vSphere management)
- ServiceNow SOAP API
- SAP Web Services
- Any other WSDL-based enterprise system

Key Design Decisions:
- Use zeep library (not pyvmomi) for system-agnostic approach
- Auto-discover operations from WSDL
- Map XML Schema types to JSON Schema for unified handling
- Session management for auth patterns like VMware's login flow
"""

from meho_app.modules.connectors.soap.client import SOAPClient

# SQLAlchemy models for SOAP operations and types
from meho_app.modules.connectors.soap.db_models import (
    SoapOperationDescriptorModel,
    SoapTypeDescriptorModel,
)
from meho_app.modules.connectors.soap.ingester import SOAPSchemaIngester
from meho_app.modules.connectors.soap.models import (
    SOAPAuthType,
    SOAPConnectorConfig,
    SOAPOperation,
    # TASK-96: Type definitions
    SOAPProperty,
    SOAPTypeDefinition,
    WSDLMetadata,
)

# Repositories
from meho_app.modules.connectors.soap.repository import (
    SoapOperationRepository,
    SoapTypeRepository,
)

__all__ = [
    "SOAPAuthType",
    "SOAPClient",
    "SOAPConnectorConfig",
    "SOAPOperation",
    # TASK-96: Type definitions
    "SOAPProperty",
    "SOAPSchemaIngester",
    "SOAPTypeDefinition",
    # SQLAlchemy models
    "SoapOperationDescriptorModel",
    # Repositories
    "SoapOperationRepository",
    "SoapTypeDescriptorModel",
    "SoapTypeRepository",
    "WSDLMetadata",
]
