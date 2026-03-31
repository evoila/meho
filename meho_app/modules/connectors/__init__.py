# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Connectors module - the primary module for external system connections.

This module provides:
- Connector management (CRUD operations for connector configurations)
- Credential management (encrypted storage of user credentials)
- Base connector interface and connection pooling
- Specific connector implementations (REST, VMware, SOAP)

Architecture:
- `connectors/` - Core connector models, schemas, repositories, service
- `connectors/rest/` - REST/OpenAPI connector type
- `connectors/vmware/` - VMware vSphere connector type
- `connectors/soap/` - SOAP/WSDL connector type
"""

# Core exports
from meho_app.modules.connectors.base import (
    BaseConnector,
    OperationDefinition,
    OperationResult,
    TypeDefinition,
)
from meho_app.modules.connectors.pool import (
    clear_connector_pool,
    execute_connector_operation,
    get_connector_instance,
    get_pooled_connector,
    register_connector_type,
)


# Lazy imports for models/schemas to avoid circular imports
def __getattr__(name: str):
    """Lazy import for heavy dependencies."""
    # Core models
    if name in (
        "ConnectorModel",
        "UserCredentialModel",
        "ConnectorOperationModel",
        "ConnectorTypeModel",
        "ProtocolType",
        "ConnectorType",
    ):
        from meho_app.modules.connectors import models

        return getattr(models, name)
    # Schemas
    elif name in ("Connector", "ConnectorCreate", "ConnectorUpdate"):
        from meho_app.modules.connectors import schemas

        return getattr(schemas, name)
    # Repositories
    elif name == "ConnectorRepository":
        from meho_app.modules.connectors.repositories import ConnectorRepository

        return ConnectorRepository
    elif name == "CredentialRepository":
        from meho_app.modules.connectors.repositories import CredentialRepository

        return CredentialRepository
    elif name == "ConnectorOperationRepository":
        from meho_app.modules.connectors.repositories import ConnectorOperationRepository

        return ConnectorOperationRepository
    elif name == "ConnectorTypeRepository":
        from meho_app.modules.connectors.repositories import ConnectorTypeRepository

        return ConnectorTypeRepository
    # Service
    elif name == "ConnectorService":
        from meho_app.modules.connectors.service import ConnectorService

        return ConnectorService
    # Router
    elif name in ("ProtocolRouter", "get_protocol_router"):
        from meho_app.modules.connectors import router

        return getattr(router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Base
    "BaseConnector",
    # Schemas (lazy)
    "Connector",
    "ConnectorCreate",
    "ConnectorModel",
    "ConnectorOperationModel",
    "ConnectorOperationRepository",
    # Repositories (lazy)
    "ConnectorRepository",
    # Service (lazy)
    "ConnectorService",
    "ConnectorType",
    "ConnectorTypeModel",
    "ConnectorTypeRepository",
    "ConnectorUpdate",
    "CredentialRepository",
    "OperationDefinition",
    "OperationResult",
    # Router (lazy)
    "ProtocolRouter",
    # Models (lazy)
    "ProtocolType",
    "TypeDefinition",
    "UserCredentialModel",
    "clear_connector_pool",
    "execute_connector_operation",
    # Pool
    "get_connector_instance",
    "get_pooled_connector",
    "get_protocol_router",
    "register_connector_type",
]
