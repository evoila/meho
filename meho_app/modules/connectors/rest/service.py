# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
REST/OpenAPI connector service.

Provides business logic for REST API connectors including:
- Connector management (CRUD)
- Endpoint discovery and search
- OpenAPI spec ingestion
- Credential management
- API endpoint calling
"""

# Import protocols for type hints (import directly to avoid circular imports)
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.modules.connectors.repositories import ConnectorRepository
from meho_app.modules.connectors.repositories.credential_repository import CredentialRepository
from meho_app.modules.connectors.repositories.operation_repository import (
    ConnectorOperationRepository,
)
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
from meho_app.modules.connectors.rest.schemas import EndpointDescriptor
from meho_app.modules.connectors.rest.session_manager import SessionManager
from meho_app.modules.connectors.rest.spec_parser import OpenAPIParser
from meho_app.modules.connectors.schemas import (
    Connector,
    ConnectorCreate,
    ConnectorUpdate,
    UserCredentialProvide,
)

if TYPE_CHECKING:
    from meho_app.protocols.openapi import (
        IConnectorRepository,
        IEndpointRepository,
        IHTTPClient,
        IOperationRepository,
        ISessionManager,
    )


class RESTConnectorService:
    """
    Service for REST/OpenAPI connector operations.

    Handles connector management, endpoint discovery, and API calls
    for REST APIs with OpenAPI specifications.

    Supports two construction patterns:

    1. Session-based (backward compatible):
        service = RESTConnectorService(session)

    2. Protocol-based (for dependency injection):
        service = RESTConnectorService.from_protocols(
            connector_repo=mock_connector_repo,
            endpoint_repo=mock_endpoint_repo,
            ...
        )
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        connector_repo: Optional["IConnectorRepository"] = None,
        endpoint_repo: Optional["IEndpointRepository"] = None,
        operation_repo: Optional["IOperationRepository"] = None,
        http_client: Optional["IHTTPClient"] = None,
        session_manager: Optional["ISessionManager"] = None,
    ):
        """
        Initialize RESTConnectorService.

        Args:
            session: AsyncSession (creates concrete implementations)
            connector_repo: Optional connector repository
            endpoint_repo: Optional endpoint repository
            operation_repo: Optional operation repository
            http_client: Optional HTTP client
            session_manager: Optional session manager
        """
        if session is not None:
            # Backward compatible: build from session
            self.session = session
            self.connector_repo = connector_repo or ConnectorRepository(session)
            self.endpoint_repo = endpoint_repo or EndpointDescriptorRepository(session)
            self.operation_repo = operation_repo or ConnectorOperationRepository(session)
            self.credential_repo = CredentialRepository(session)
            self.http_client = http_client or GenericHTTPClient()
            self.session_manager = session_manager or SessionManager(self.credential_repo)
            self.spec_parser = OpenAPIParser()
        elif connector_repo is not None:
            # Protocol-based construction
            self.session = None
            self.connector_repo = connector_repo
            self.endpoint_repo = endpoint_repo
            self.operation_repo = operation_repo
            self.credential_repo = None
            self.http_client = http_client or GenericHTTPClient()
            self.session_manager = session_manager
            self.spec_parser = OpenAPIParser()
        else:
            raise ValueError(
                "RESTConnectorService requires either 'session' or 'connector_repo' argument"
            )

    @classmethod
    def from_protocols(
        cls,
        connector_repo: "IConnectorRepository",
        endpoint_repo: Optional["IEndpointRepository"] = None,
        operation_repo: Optional["IOperationRepository"] = None,
        http_client: Optional["IHTTPClient"] = None,
        session_manager: Optional["ISessionManager"] = None,
    ) -> "RESTConnectorService":
        """
        Create RESTConnectorService from protocol implementations.

        This is the preferred constructor for testing and dependency injection.

        Args:
            connector_repo: Connector repository implementation
            endpoint_repo: Optional endpoint repository
            operation_repo: Optional operation repository
            http_client: Optional HTTP client
            session_manager: Optional session manager

        Returns:
            Configured RESTConnectorService instance
        """
        return cls(
            session=None,
            connector_repo=connector_repo,
            endpoint_repo=endpoint_repo,
            operation_repo=operation_repo,
            http_client=http_client,
            session_manager=session_manager,
        )

    # Connector operations
    async def create_connector(
        self,
        data: ConnectorCreate,
        tenant_id: str,
    ) -> Connector:
        """Create a new connector."""
        return await self.connector_repo.create(data, tenant_id)

    async def get_connector(self, connector_id: str) -> Connector | None:
        """Get a connector by ID."""
        return await self.connector_repo.get_by_id(connector_id)

    async def update_connector(
        self,
        connector_id: str,
        data: ConnectorUpdate,
    ) -> Connector:
        """Update a connector."""
        return await self.connector_repo.update(connector_id, data)

    async def list_connectors(
        self,
        tenant_id: str,
        is_active: bool | None = None,
        limit: int = 50,
    ) -> list[Connector]:
        """List connectors for a tenant."""
        return await self.connector_repo.list_by_tenant(
            tenant_id=tenant_id,
            is_active=is_active,
            limit=limit,
        )

    async def delete_connector(self, connector_id: str) -> bool:
        """Delete a connector."""
        return await self.connector_repo.delete(connector_id)

    # Endpoint operations
    async def search_endpoints(
        self,
        query: str,
        connector_id: str | None = None,
        tenant_id: str | None = None,
        top_k: int = 10,
    ) -> list[EndpointDescriptor]:
        """Search for endpoints matching a query."""
        return await self.endpoint_repo.search(
            query=query,
            connector_id=connector_id,
            tenant_id=tenant_id,
            top_k=top_k,
        )

    async def get_endpoint(self, endpoint_id: str) -> EndpointDescriptor | None:
        """Get an endpoint by ID."""
        return await self.endpoint_repo.get_by_id(endpoint_id)

    async def list_endpoints(
        self,
        connector_id: str,
        limit: int = 100,
    ) -> list[EndpointDescriptor]:
        """List endpoints for a connector."""
        return await self.endpoint_repo.list_by_connector(connector_id, limit)

    # Spec operations
    async def ingest_openapi_spec(
        self,
        connector_id: str,
        spec_content: bytes,
        filename: str,
    ) -> dict[str, Any]:
        """Ingest an OpenAPI spec for a connector."""
        # Parse spec
        spec_dict = await self.spec_parser.parse(spec_content, filename)

        # Create endpoints
        endpoints = await self.endpoint_repo.create_from_spec(connector_id, spec_dict)

        return {
            "connector_id": connector_id,
            "endpoints_created": len(endpoints),
            "spec_version": spec_dict.get("openapi") or spec_dict.get("swagger"),
        }

    # Credential operations
    async def store_credentials(
        self,
        tenant_id: str,
        user_id: str,
        connector_id: str,
        credentials: dict[str, Any],
    ) -> Any:
        """Store user credentials for a connector."""
        if self.credential_repo is None:
            raise ValueError("Credential repository not available")
        credential = UserCredentialProvide(
            connector_id=connector_id,
            credentials=credentials,
        )
        return await self.credential_repo.store_credentials(user_id, credential)

    async def get_credentials(
        self,
        tenant_id: str,
        user_id: str,
        connector_id: str,
    ) -> dict[str, Any] | None:
        """Get user credentials for a connector."""
        if self.credential_repo is None:
            return None
        return await self.credential_repo.get_credentials(user_id, connector_id)

    async def delete_credentials(
        self,
        tenant_id: str,
        user_id: str,
        connector_id: str,
    ) -> bool:
        """Delete user credentials for a connector."""
        if self.credential_repo is None:
            return False
        return await self.credential_repo.delete_credentials(user_id, connector_id)

    # API call operations
    async def call_endpoint(
        self,
        connector_id: str,
        endpoint_id: str,
        tenant_id: str,
        user_id: str,
        path_params: dict[str, str] | None = None,
        query_params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an external API endpoint."""
        # Get connector and endpoint
        connector = await self.connector_repo.get_by_id(connector_id)
        endpoint = await self.endpoint_repo.get_by_id(endpoint_id)

        if not connector or not endpoint:
            raise ValueError("Connector or endpoint not found")

        # Get credentials and ensure session
        if self.credential_repo:
            credentials = await self.credential_repo.get_credentials(user_id, connector_id)
        else:
            credentials = None

        if self.session_manager:
            session = await self.session_manager.ensure_session(connector, credentials)
        else:
            session = None

        # Make the call
        result = await self.http_client.call(
            connector=connector,
            endpoint=endpoint,
            session=session,
            path_params=path_params or {},
            query_params=query_params or {},
            body=body,
        )

        return {
            "status_code": result.status_code,
            "data": result.data,
            "headers": dict(result.headers) if hasattr(result, "headers") else {},
        }


# Backward compatibility aliases
OpenAPIService = RESTConnectorService


def get_openapi_service(session: AsyncSession) -> RESTConnectorService:
    """Factory function for getting a RESTConnectorService instance."""
    return RESTConnectorService(session)


def get_rest_connector_service(session: AsyncSession) -> RESTConnectorService:
    """Factory function for getting a RESTConnectorService instance."""
    return RESTConnectorService(session)


__all__ = [
    "OpenAPIService",  # Backward compatibility alias
    "RESTConnectorService",
    "get_openapi_service",
    "get_rest_connector_service",
]
