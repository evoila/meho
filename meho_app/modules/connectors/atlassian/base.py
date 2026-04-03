# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Atlassian HTTP Connector Base Class.

Abstract base between BaseConnector and concrete Atlassian connectors
(Jira, Confluence). Manages httpx.AsyncClient lifecycle with email:api_token
Basic Auth, rate limit handling (429 retry), and Atlassian-specific error mapping.

Pattern mirrors ObservabilityHTTPConnector but uses Basic Auth with
base64-encoded email:api_token per Atlassian Cloud API requirements.
"""

import asyncio
import base64
from typing import Any

import httpx

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import BaseConnector

logger = get_logger(__name__)


class AtlassianHTTPConnector(BaseConnector):
    """
    Shared base for Atlassian Cloud connectors (Jira, Confluence).

    Manages httpx.AsyncClient with Basic Auth (email:api_token),
    automatic 429 rate-limit retry, and Atlassian error mapping.
    Subclasses implement test_connection(), execute(), get_operations(),
    and get_types().

    Auth: Basic Auth with base64-encoded email:api_token
    Rate limits: Automatic single retry on 429 with Retry-After header
    """

    def __init__(
        self,
        connector_id: str,
        config: dict[str, Any],
        credentials: dict[str, Any],
    ) -> None:
        super().__init__(connector_id, config, credentials)
        self._client: httpx.AsyncClient | None = None

        # Configuration
        self.base_url = config.get("base_url", "").rstrip("/")
        self.timeout = config.get("timeout", 30.0)

    def _build_headers(self) -> dict[str, str]:
        """
        Build HTTP headers with Basic Auth for Atlassian Cloud.

        Atlassian Cloud uses email:api_token encoded as Basic Auth,
        not username:password. The api_token is generated from
        https://id.atlassian.com/manage-profile/security/api-tokens
        """
        email = self.credentials.get("email", "")
        api_token = self.credentials.get("api_token", "")
        raw = f"{email}:{api_token}"
        encoded = base64.b64encode(raw.encode()).decode()

        return {
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def connect(self) -> bool:
        """Create httpx.AsyncClient with pre-configured Basic Auth."""
        if self._is_connected and self._client:
            return True

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._build_headers(),
            timeout=self.timeout,
        )
        self._is_connected = True
        logger.info(
            "Atlassian connector connected",
            extra={"connector_id": self.connector_id, "base_url": self.base_url},
        )
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
        """
        Execute GET request with 429 rate-limit retry.

        If the response is 429 (Too Many Requests), reads the Retry-After
        header (defaulting to 5s) and retries once.
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.get(path, params=params)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            logger.warning(
                f"Rate limited (429), retrying after {retry_after}s",
                extra={"connector_id": self.connector_id, "path": path},
            )
            await asyncio.sleep(retry_after)
            response = await self._client.get(path, params=params)

        response.raise_for_status()
        return dict(response.json())

    async def _post(self, path: str, json: Any | None = None) -> dict:
        """
        Execute POST request with 429 rate-limit retry.

        Same retry pattern as _get for rate-limited POST requests.
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.post(path, json=json)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            logger.warning(
                f"Rate limited (429), retrying after {retry_after}s",
                extra={"connector_id": self.connector_id, "path": path},
            )
            await asyncio.sleep(retry_after)
            response = await self._client.post(path, json=json)

        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return dict(response.json())

    async def _put(self, path: str, json: Any | None = None) -> dict:
        """
        Execute PUT request with 429 rate-limit retry.

        Needed for Confluence page updates and Jira issue updates.
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.put(path, json=json)

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            logger.warning(
                f"Rate limited (429), retrying after {retry_after}s",
                extra={"connector_id": self.connector_id, "path": path},
            )
            await asyncio.sleep(retry_after)
            response = await self._client.put(path, json=json)

        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return dict(response.json())

    def _map_http_error(self, e: Exception) -> str:
        """
        Map httpx/HTTP exceptions to OperationResult error codes.

        Extends the ObservabilityHTTPConnector mapping with Atlassian-specific
        status codes: 404 NOT_FOUND (issue/page not found),
        409 CONFLICT (concurrent transition, version conflict).
        """
        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code
            if status == 401:
                return "AUTHENTICATION_FAILED"
            elif status == 403:
                return "PERMISSION_DENIED"
            elif status == 404:
                return "NOT_FOUND"
            elif status == 409:
                return "CONFLICT"
            elif status in (400, 422):
                return "INVALID_REQUEST"
            elif status == 429:
                return "RATE_LIMITED"
            elif status == 503:
                return "SERVICE_UNAVAILABLE"
            elif status >= 500:
                return "SERVER_ERROR"
        elif isinstance(e, httpx.TimeoutException):
            return "TIMEOUT"
        elif isinstance(e, httpx.ConnectError):
            return "CONNECTION_FAILED"
        return "INTERNAL_ERROR"

    async def __aenter__(self) -> "AtlassianHTTPConnector":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()
