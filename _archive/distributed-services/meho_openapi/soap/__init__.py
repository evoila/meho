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

from meho_openapi.soap.ingester import SOAPSchemaIngester
from meho_openapi.soap.client import SOAPClient
from meho_openapi.soap.models import (
    SOAPOperation,
    SOAPConnectorConfig,
    SOAPAuthType,
    WSDLMetadata,
    # TASK-96: Type definitions
    SOAPProperty,
    SOAPTypeDefinition,
)

__all__ = [
    "SOAPSchemaIngester",
    "SOAPClient",
    "SOAPOperation",
    "SOAPConnectorConfig",
    "SOAPAuthType",
    "WSDLMetadata",
    # TASK-96: Type definitions
    "SOAPProperty",
    "SOAPTypeDefinition",
]

