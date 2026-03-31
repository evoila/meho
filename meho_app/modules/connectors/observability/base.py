# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Shared Observability HTTP Connector Base Class.

Abstract base between BaseConnector and concrete observability connectors
(Prometheus, Loki, Tempo, Alertmanager). Manages httpx.AsyncClient lifecycle
with pre-configured auth (basic/bearer/none), TLS verification, and error mapping.
"""

from typing import Any

import httpx

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import BaseConnector

logger = get_logger(__name__)


class ObservabilityHTTPConnector(BaseConnector):
    """
    Shared base for HTTP-based observability connectors.

    Manages httpx.AsyncClient with connection pooling, configurable auth,
    and automatic error mapping. Subclasses implement test_connection(),
    execute(), get_operations(), and get_types().

    Auth modes:
    - "none": No authentication (internal Prometheus/Loki/etc.)
    - "basic": HTTP Basic Auth (username/password via nginx/Apache proxy)
    - "bearer": Bearer token (OAuth2 proxy, service mesh)
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ):
        super().__init__(connector_id, config, credentials)
        self._client: httpx.AsyncClient | None = None

        # Configuration
        self.base_url = config.get("base_url", "").rstrip("/")
        self.verify_ssl = not config.get("skip_tls_verification", False)
        self.timeout = config.get("timeout", 30.0)

        # Auth configuration
        self.auth_type = config.get("auth_type", "none")  # basic, bearer, none

    def _build_auth(self) -> httpx.Auth | None:
        """Build httpx auth from credentials."""
        if self.auth_type == "basic":
            return httpx.BasicAuth(
                self.credentials.get("username", ""),
                self.credentials.get("password", ""),
            )
        return None

    def _build_headers(self) -> dict[str, str]:
        """Build default headers including bearer auth."""
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.auth_type == "bearer":
            token = self.credentials.get("token", "") or self.credentials.get("access_token", "")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def connect(self) -> bool:
        """Create httpx.AsyncClient with pre-configured auth."""
        if self._is_connected and self._client:
            return True

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=self._build_auth(),
            headers=self._build_headers(),
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        """Close httpx client."""
        if self._client:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.warning(f"Error closing httpx client: {e}")
            finally:
                self._client = None
        self._is_connected = False

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Execute GET request, raise on HTTP error, return JSON."""
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking
        response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def _post(
        self, path: str, json: Any | None = None, params: dict[str, Any] | None = None
    ) -> dict:
        """Execute POST request, raise on HTTP error, return JSON."""
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking
        response = await self._client.post(path, json=json, params=params)
        response.raise_for_status()
        return response.json()

    def _map_http_error(self, e: Exception) -> str:
        """Map httpx/HTTP exceptions to OperationResult error codes."""
        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code
            if status == 401:
                return "AUTHENTICATION_FAILED"
            elif status == 403:
                return "PERMISSION_DENIED"
            elif status in (400, 422):
                return "INVALID_REQUEST"
            elif status == 503:
                return "SERVICE_UNAVAILABLE"
            elif status >= 500:
                return "SERVER_ERROR"
        elif isinstance(e, httpx.TimeoutException):
            return "TIMEOUT"
        elif isinstance(e, httpx.ConnectError):
            return "CONNECTION_FAILED"
        return "INTERNAL_ERROR"

    async def __aenter__(self) -> "ObservabilityHTTPConnector":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()
