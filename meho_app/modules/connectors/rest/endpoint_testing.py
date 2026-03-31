# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
OpenAPI Endpoint Testing Service - Shared service for endpoint testing operations.

Provides a single implementation for:
- Connector management
- Endpoint testing
- API calls with authentication
- SESSION auth handling

This service is used by:
- routes_workflow_builder.py (test-endpoint, discover-endpoint)
- routes_connectors.py (test-endpoint, test-connection)
- MEHODependencies (call_endpoint for agent execution)

DRY Principle: Single source of truth for endpoint calling logic.
"""

# mypy: disable-error-code="no-untyped-def,arg-type,return-value"
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from meho_app.core.auth_context import UserContext
from meho_app.core.otel import get_logger
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.connectors.repositories.connector_repository import ConnectorRepository
from meho_app.modules.connectors.repositories.credential_repository import UserCredentialRepository
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
from meho_app.modules.connectors.rest.models import EndpointDescriptorModel as EndpointDescriptor
from meho_app.modules.connectors.rest.repository import EndpointDescriptorRepository
from meho_app.modules.connectors.rest.schemas import EndpointFilter
from meho_app.modules.connectors.schemas import Connector as ConnectorSchema

logger = get_logger(__name__)


@dataclass
class TestEndpointResult:
    """Result from testing an endpoint."""

    success: bool
    status_code: int | None = None
    data: Any | None = None
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class CallEndpointResult:
    """Result from calling an endpoint."""

    status_code: int
    data: Any
    duration_ms: float


class OpenAPIService:
    """
    Shared service for OpenAPI operations.

    Encapsulates:
    - Connector/endpoint retrieval with tenant isolation
    - Credential management (SYSTEM vs USER_PROVIDED)
    - SESSION auth state handling
    - HTTP client calls

    Usage:
        service = OpenAPIService(session)
        result = await service.test_endpoint(user, connector_id, endpoint_id)
    """

    def __init__(self, session: AsyncSession):
        """
        Initialize service with database session.

        Args:
            session: SQLAlchemy async session for database operations
        """
        self.session = session
        self.connector_repo = ConnectorRepository(session)
        self.endpoint_repo = EndpointDescriptorRepository(session)
        self.cred_repo = UserCredentialRepository(session)
        self.http_client = GenericHTTPClient()

    def connector_to_schema(self, connector: ConnectorModel) -> ConnectorSchema:
        """
        Convert database Connector model to Pydantic ConnectorSchema.

        IMPORTANT: This ensures ALL fields are included, especially SESSION auth fields.
        This prevents bugs where fields like login_url are forgotten during manual conversion.

        Args:
            connector: Database model instance

        Returns:
            ConnectorSchema with all fields populated
        """
        return ConnectorSchema(
            id=str(connector.id),
            tenant_id=connector.tenant_id,
            name=connector.name,
            base_url=connector.base_url,
            auth_type=connector.auth_type,
            auth_config=connector.auth_config or {},
            credential_strategy=connector.credential_strategy or "SYSTEM",
            description=connector.description,
            allowed_methods=connector.allowed_methods or [],
            blocked_methods=connector.blocked_methods or [],
            default_safety_level=connector.default_safety_level or "safe",
            is_active=connector.is_active,
            created_at=connector.created_at,
            updated_at=connector.updated_at,
            # SESSION auth fields - CRITICAL for SESSION auth to work
            login_url=connector.login_url,
            login_method=connector.login_method,
            login_config=connector.login_config,
        )

    async def get_connector(self, connector_id: str, tenant_id: str) -> ConnectorModel | None:
        """
        Get connector with tenant isolation.

        Args:
            connector_id: UUID of connector
            tenant_id: Tenant ID for access control

        Returns:
            Connector model if found and accessible, None otherwise
        """
        connector = await self.connector_repo.get_connector(connector_id)
        if connector and connector.tenant_id == tenant_id:
            return connector
        return None

    async def get_endpoint(self, endpoint_id: str) -> EndpointDescriptor | None:
        """
        Get endpoint by ID.

        Args:
            endpoint_id: UUID of endpoint

        Returns:
            EndpointDescriptor if found, None otherwise
        """
        return await self.endpoint_repo.get_endpoint(endpoint_id)

    async def get_credentials(
        self, user_context: UserContext, connector: ConnectorModel
    ) -> dict[str, Any] | None:
        """
        Get credentials for a connector based on its credential strategy.

        Handles:
        - SYSTEM: Returns auth_config from connector
        - USER_PROVIDED: Fetches from user credentials table
        - NONE: Returns None

        Args:
            user_context: Current user context
            connector: Connector model

        Returns:
            Credentials dict or None
        """
        if connector.auth_type == "NONE":
            return None

        if connector.credential_strategy == "USER_PROVIDED":
            return await self.cred_repo.get_credentials(
                user_id=user_context.user_id, connector_id=str(connector.id)
            )
        elif connector.credential_strategy == "SYSTEM":
            return connector.auth_config

        return None

    async def get_session_state(
        self, user_context: UserContext, connector: ConnectorModel
    ) -> dict[str, Any]:
        """
        Get SESSION auth state if applicable.

        Args:
            user_context: Current user context
            connector: Connector model

        Returns:
            Dict with session_token, session_expires_at, refresh_token, refresh_expires_at
            Empty dict if not SESSION auth
        """
        if connector.auth_type != "SESSION":
            return {}

        session_state = await self.cred_repo.get_session_state(
            user_id=user_context.user_id, connector_id=str(connector.id)
        )

        if session_state:
            return {
                "session_token": session_state.get("session_token"),
                "session_expires_at": session_state.get("session_expires_at"),
                "refresh_token": session_state.get("refresh_token"),
                "refresh_expires_at": session_state.get("refresh_expires_at"),
            }

        return {}

    async def test_endpoint(
        self,
        user_context: UserContext,
        connector_id: str,
        endpoint_id: str,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> TestEndpointResult:
        """
        Test an endpoint with proper authentication.

        Single implementation for endpoint testing used by:
        - Workflow builder (/api/workflow-definitions/test-endpoint)
        - Connectors page (/api/connectors/{id}/endpoints/{id}/test)

        Args:
            user_context: Current user context for auth and tenant isolation
            connector_id: UUID of connector
            endpoint_id: UUID of endpoint
            path_params: Path parameters for the endpoint
            query_params: Query parameters
            body: Request body

        Returns:
            TestEndpointResult with success/failure info
        """
        logger.info(f"🧪 TEST_ENDPOINT: Testing {connector_id}/{endpoint_id}")

        try:
            # 1. Get and validate connector
            connector = await self.get_connector(connector_id, user_context.tenant_id)
            if not connector:
                return TestEndpointResult(
                    success=False, error="Connector not found or access denied"
                )

            # 2. Get endpoint
            endpoint = await self.get_endpoint(endpoint_id)
            if not endpoint:
                return TestEndpointResult(success=False, error="Endpoint not found")

            # 3. Get credentials
            credentials = await self.get_credentials(user_context, connector)

            # 4. Get session state for SESSION auth
            session_state = await self.get_session_state(user_context, connector)

            # 5. Convert to schema (ensures all fields including SESSION auth)
            connector_schema = self.connector_to_schema(connector)

            # 6. Call endpoint
            start_time = time.time()

            status_code, response_data = await self.http_client.call_endpoint(
                connector=connector_schema,
                endpoint=endpoint,
                path_params=path_params or {},
                query_params=query_params or {},
                body=body,
                user_credentials=credentials,
                session_token=session_state.get("session_token"),
                session_expires_at=session_state.get("session_expires_at"),
                refresh_token=session_state.get("refresh_token"),
                refresh_expires_at=session_state.get("refresh_expires_at"),
            )

            duration_ms = (time.time() - start_time) * 1000

            logger.info(f"✅ TEST_ENDPOINT: Success - {status_code} in {duration_ms:.0f}ms")

            return TestEndpointResult(
                success=True, status_code=status_code, data=response_data, duration_ms=duration_ms
            )

        except Exception as e:
            logger.error(f"❌ TEST_ENDPOINT: Failed - {e}")
            return TestEndpointResult(success=False, error=str(e))

    async def call_endpoint(
        self,
        user_context: UserContext,
        connector_id: str,
        endpoint_id: str,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        body: Any | None = None,
        on_session_update: Callable[..., Awaitable[None]] | None = None,
    ) -> CallEndpointResult:
        """
        Call an endpoint with full authentication handling.

        Unlike test_endpoint(), this:
        - Raises exceptions on failure (for use in workflows)
        - Supports session update callbacks
        - Does NOT handle large response summarization (caller's responsibility)

        Args:
            user_context: Current user context
            connector_id: UUID of connector
            endpoint_id: UUID of endpoint
            path_params: Path parameters
            query_params: Query parameters
            body: Request body
            on_session_update: Callback for SESSION auth token refresh

        Returns:
            CallEndpointResult with status_code and data

        Raises:
            ValueError: If connector/endpoint not found or auth fails
        """
        logger.info(f"🌐 CALL_ENDPOINT: Calling {connector_id}/{endpoint_id}")

        # 1. Get and validate connector
        connector = await self.get_connector(connector_id, user_context.tenant_id)
        if not connector:
            raise ValueError(f"Connector {connector_id} not found")

        # 2. Get endpoint
        endpoint = await self.get_endpoint(endpoint_id)
        if not endpoint:
            raise ValueError(f"Endpoint {endpoint_id} not found")

        # 3. Validate endpoint belongs to connector
        if endpoint.connector_id != str(connector.id):
            raise ValueError(f"Endpoint {endpoint_id} does not belong to connector {connector_id}")

        # 4. Get credentials
        credentials = await self.get_credentials(user_context, connector)
        if connector.credential_strategy == "USER_PROVIDED" and not credentials:
            raise ValueError(f"No credentials found for connector {connector.name}")

        # 5. Get session state for SESSION auth
        session_state = await self.get_session_state(user_context, connector)

        # 6. Build session update callback if needed
        async def handle_session_update(
            token: str,
            expires_at: datetime | None,
            state: str,
            refresh: str | None = None,
            refresh_expires: datetime | None = None,
        ):
            """Update session state in database."""
            await self.cred_repo.update_session_state(
                user_id=user_context.user_id,
                connector_id=connector_id,
                session_token=token,
                session_expires_at=expires_at,
                session_state=state,
                refresh_token=refresh,
                refresh_expires_at=refresh_expires,
            )
            # Also call caller's callback if provided
            if on_session_update:
                await on_session_update(token, expires_at, state, refresh, refresh_expires)

        # 7. Convert to schema
        connector_schema = self.connector_to_schema(connector)

        # 8. Call endpoint
        start_time = time.time()

        status_code, response_data = await self.http_client.call_endpoint(
            connector=connector_schema,
            endpoint=endpoint,
            path_params=path_params or {},
            query_params=query_params or {},
            body=body,
            user_credentials=credentials,
            session_token=session_state.get("session_token"),
            session_expires_at=session_state.get("session_expires_at"),
            refresh_token=session_state.get("refresh_token"),
            refresh_expires_at=session_state.get("refresh_expires_at"),
            on_session_update=handle_session_update if connector.auth_type == "SESSION" else None,
        )

        duration_ms = (time.time() - start_time) * 1000

        logger.info(f"✅ CALL_ENDPOINT: {status_code} in {duration_ms:.0f}ms")

        return CallEndpointResult(
            status_code=status_code, data=response_data, duration_ms=duration_ms
        )

    async def find_test_endpoint(self, connector_id: str) -> EndpointDescriptor | None:
        """
        Find a safe endpoint for connection testing.

        Prefers endpoints like /health, /status, /version, /ping, /api.
        Falls back to first available GET endpoint WITHOUT path parameters.

        Args:
            connector_id: UUID of connector

        Returns:
            EndpointDescriptor suitable for testing, or None
        """
        import re

        # Get enabled GET endpoints (fetch more to find good candidates)
        endpoints = await self.endpoint_repo.list_endpoints(
            EndpointFilter(connector_id=connector_id, method="GET", is_enabled=True, limit=100)
        )

        if not endpoints:
            return None

        def has_path_params(path: str) -> bool:
            """Check if path has {param} placeholders."""
            return bool(re.search(r"\{[^}]+\}", path))

        # Priority 1: Health/status/version endpoints without path params
        preferred_keywords = ["/health", "/status", "/version", "/ping", "/api/v1", "/apis"]
        for ep in endpoints:
            path_lower = ep.path.lower()
            if any(keyword in path_lower for keyword in preferred_keywords):  # noqa: SIM102 -- readability preferred over collapse
                if not has_path_params(ep.path):
                    return ep

        # Priority 2: Any endpoint without path parameters (shortest path first)
        no_param_endpoints = [ep for ep in endpoints if not has_path_params(ep.path)]
        if no_param_endpoints:
            # Sort by path length to prefer simpler endpoints like /api/v1 over /api/v1/namespaces
            no_param_endpoints.sort(key=lambda ep: len(ep.path))
            return no_param_endpoints[0]

        # Priority 3: Fall back to first GET endpoint (may fail due to missing params)
        # This is a last resort - the test will fail with "missing path parameters"
        return endpoints[0]


# Singleton instance (optional, for cases where session is managed externally)
_service_instance: OpenAPIService | None = None


def get_openapi_service_singleton(session: AsyncSession) -> OpenAPIService:
    """
    Get or create OpenAPIService singleton.

    Note: For FastAPI, prefer using Depends() with a factory function instead.
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = OpenAPIService(session)
    return _service_instance


def reset_openapi_service():
    """Reset singleton (for testing)."""
    global _service_instance
    _service_instance = None
