# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Protocol definitions for the Connectors module.

These protocols define the interfaces for connector management,
endpoint discovery, and HTTP client operations.
"""

from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

# Avoid circular imports - use string annotations
if TYPE_CHECKING:
    from meho_app.modules.connectors.rest.schemas import EndpointDescriptor
    from meho_app.modules.connectors.schemas import (
        Connector,
        ConnectorCreate,
        ConnectorUpdate,
    )


@runtime_checkable
class IConnectorRepository(Protocol):
    """
    Protocol for connector CRUD operations.

    Implementations:
        - ConnectorRepository (PostgreSQL)
    """

    async def create_connector(self, connector: "ConnectorCreate") -> "Connector":
        """Create a new connector."""
        ...

    async def get_connector(
        self, connector_id: str, tenant_id: str | None = None
    ) -> Optional["Connector"]:
        """Get connector by ID, optionally filtered by tenant."""
        ...

    async def list_connectors(self, tenant_id: str, active_only: bool = True) -> list["Connector"]:
        """List connectors for a tenant."""
        ...

    async def update_connector(
        self, connector_id: str, update: "ConnectorUpdate", tenant_id: str | None = None
    ) -> Optional["Connector"]:
        """Update connector configuration."""
        ...

    async def delete_connector(self, connector_id: str, tenant_id: str | None = None) -> bool:
        """Delete a connector. Returns True if deleted."""
        ...


@runtime_checkable
class IEndpointRepository(Protocol):
    """
    Protocol for endpoint descriptor operations.

    Implementations:
        - EndpointDescriptorRepository (PostgreSQL)
    """

    async def search(
        self,
        query: str,
        connector_id: str | None = None,
        tenant_id: str | None = None,
        top_k: int = 10,
    ) -> list["EndpointDescriptor"]:
        """Search endpoints by natural language query."""
        ...

    async def get_by_id(self, endpoint_id: str) -> Optional["EndpointDescriptor"]:
        """Get endpoint by ID."""
        ...

    async def list_by_connector(
        self, connector_id: str, limit: int = 100
    ) -> list["EndpointDescriptor"]:
        """List all endpoints for a connector."""
        ...

    async def create_endpoint(self, endpoint: "EndpointDescriptor") -> "EndpointDescriptor":
        """Create a new endpoint descriptor."""
        ...

    async def delete_by_connector(self, connector_id: str) -> int:
        """Delete all endpoints for a connector. Returns count deleted."""
        ...


@runtime_checkable
class IOperationRepository(Protocol):
    """
    Protocol for connector operation management (VMware, etc.).

    Implementations:
        - ConnectorOperationRepository (PostgreSQL)
    """

    async def search_operations(
        self,
        query: str,
        connector_id: str | None = None,
        category: str | None = None,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Search operations by natural language query."""
        ...

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Get operation by ID."""
        ...

    async def list_by_connector(
        self, connector_id: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """List operations for a connector."""
        ...

    async def register_operations(self, connector_id: str, operations: list[dict[str, Any]]) -> int:
        """Register operations for a connector. Returns count registered."""
        ...


@runtime_checkable
class IHTTPClient(Protocol):
    """
    Protocol for generic HTTP client operations.

    Implementations:
        - GenericHTTPClient (httpx)
    """

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,  # noqa: ASYNC109 -- timeout handled at caller level
    ) -> dict[str, Any]:
        """
        Make an HTTP request.

        Returns:
            Dict with 'status_code', 'headers', 'body' keys
        """
        ...

    async def get(
        self, url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a GET request."""
        ...

    async def post(
        self, url: str, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Make a POST request."""
        ...


@runtime_checkable
class ISessionManager(Protocol):
    """
    Protocol for managing authenticated sessions with connectors.

    Implementations:
        - SessionManager
    """

    async def get_session(
        self, connector_id: str, user_id: str, tenant_id: str
    ) -> dict[str, Any] | None:
        """Get an active session for a connector/user."""
        ...

    async def create_session(
        self, connector: "Connector", user_id: str, credentials: dict[str, str]
    ) -> dict[str, Any]:
        """Create a new authenticated session."""
        ...

    async def refresh_session(self, session_id: str) -> dict[str, Any] | None:
        """Refresh an expiring session."""
        ...

    async def invalidate_session(self, session_id: str) -> bool:
        """Invalidate a session."""
        ...
