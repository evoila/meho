# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Generic HTTP client for calling REST API endpoints.

Handles authentication and request construction dynamically.
Emits HTTP call events for deep observability when a transcript collector is available.
"""

import base64
import json
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from meho_app.core.errors import UpstreamApiError
from meho_app.core.otel import get_logger
from meho_app.modules.agents.persistence.event_context import get_transcript_collector
from meho_app.modules.connectors.rest.schemas import EndpointDescriptor
from meho_app.modules.connectors.rest.session_manager import SessionManager
from meho_app.modules.connectors.schemas import Connector

logger = get_logger(__name__)

# Headers that should be redacted for security
SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "cookie",
        "set-cookie",
        "x-auth-token",
        "api-key",
        "bearer",
    }
)

# Maximum payload size to store in transcript (2KB)
MAX_PAYLOAD_SIZE = 2000


class GenericHTTPClient:
    """Generic HTTP client for calling any REST API.

    Automatically emits HTTP call events to the transcript collector
    if one is available in the current async context.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self.session_manager = SessionManager(timeout=timeout)

    def _sanitize_headers(self, headers: dict[str, str]) -> dict[str, str]:
        """Remove sensitive header values for safe logging.

        Args:
            headers: Original headers dict.

        Returns:
            Headers with sensitive values replaced with '***'.
        """
        return {k: ("***" if k.lower() in SENSITIVE_HEADERS else v) for k, v in headers.items()}

    def _truncate_payload(self, data: Any) -> str:
        """Truncate large payloads to avoid database bloat.

        Args:
            data: Response data (dict, list, or string).

        Returns:
            JSON string, truncated if necessary.
        """
        if data is None:
            return ""

        try:
            text = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
        except (TypeError, ValueError):
            text = str(data)

        if len(text) > MAX_PAYLOAD_SIZE:
            return text[:MAX_PAYLOAD_SIZE] + "... [truncated]"
        return text

    async def _emit_http_event(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        request_body: Any,
        response_body: Any,
        status_code: int,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        """Emit an HTTP call event to the transcript collector if available.

        This method is non-blocking and fails silently if no collector is available
        or if event emission fails.

        Args:
            method: HTTP method (GET, POST, etc.).
            url: Request URL.
            headers: Request headers (will be sanitized).
            request_body: Request body (will be truncated).
            response_body: Response body (will be truncated).
            status_code: HTTP status code.
            duration_ms: Request duration in milliseconds.
            error: Error message if request failed.
        """
        try:
            collector = get_transcript_collector()
            if collector is None:
                return

            # Sanitize and truncate data for safe storage
            safe_headers = self._sanitize_headers(headers)
            safe_request = self._truncate_payload(request_body)
            safe_response = self._truncate_payload(response_body)

            # Create event
            summary = f"{method} {url} -> {status_code}"
            if error:
                summary = f"{method} {url} -> ERROR: {error[:50]}"

            event = collector.create_operation_event(
                summary=summary,
                method=method,
                url=url,
                headers=safe_headers,
                request_body=safe_request if safe_request else None,
                response_body=safe_response if safe_response else None,
                status_code=status_code,
                duration_ms=duration_ms,
            )

            await collector.add(event)

        except Exception as e:
            # Non-blocking - just log the error
            logger.debug(f"Failed to emit HTTP event: {e}")

    async def call_endpoint(
        self,
        connector: Connector,
        endpoint: EndpointDescriptor,
        path_params: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        user_credentials: dict[str, str] | None = None,
        session_token: str | None = None,
        session_expires_at: datetime | None = None,
        refresh_token: str | None = None,
        refresh_expires_at: datetime | None = None,
        on_session_update: Callable[[str, datetime, str, str | None, datetime | None], None]
        | None = None,
    ) -> tuple[int, Any]:
        """
        Call an API endpoint.

        Args:
            connector: Connector configuration
            endpoint: Endpoint descriptor
            path_params: Path parameters
            query_params: Query parameters
            body: Request body
            user_credentials: User-specific credentials (for USER_PROVIDED strategy)
            session_token: Current session token (for SESSION auth)
            session_expires_at: Session token expiry (for SESSION auth)
            refresh_token: Current refresh token (for SESSION auth)
            refresh_expires_at: Refresh token expiry (for SESSION auth)
            on_session_update: Callback to update session state

        Returns:
            (status_code, response_data)
        """
        path_params = path_params or {}
        query_params = query_params or {}

        logger.info(f"\n{'=' * 80}")
        logger.info("HTTP_CLIENT: Making API request")
        logger.info(f"   Connector: {connector.name}")
        logger.info(f"   Endpoint: {endpoint.method} {endpoint.path}")
        logger.info(f"   Auth Type: {connector.auth_type}")
        logger.info(f"{'=' * 80}")

        # Handle SESSION auth - auto-login if needed
        if connector.auth_type == "SESSION":
            if not user_credentials:
                raise ValueError("SESSION auth requires user_credentials")

            try:
                (
                    new_token,
                    new_refresh,
                    new_expires,
                    new_refresh_expires,
                    new_state,
                ) = await self.session_manager.login(
                    connector=connector,
                    credentials=user_credentials,
                    session_token=session_token,
                    session_expires_at=session_expires_at,
                    refresh_token=refresh_token,
                    refresh_expires_at=refresh_expires_at,
                )

                # Update session state if callback provided
                if on_session_update and (
                    new_token != session_token or new_expires != session_expires_at
                ):
                    import inspect

                    if inspect.iscoroutinefunction(on_session_update):
                        await on_session_update(
                            new_token, new_expires, new_state, new_refresh, new_refresh_expires
                        )
                    else:
                        on_session_update(
                            new_token, new_expires, new_state, new_refresh, new_refresh_expires
                        )

                session_token = new_token
                session_expires_at = new_expires

            except Exception as e:
                logger.error(f"Session login failed: {e}")
                raise ValueError(f"Failed to establish session: {e}") from e

        # Build URL
        url = self._build_url(connector.base_url, endpoint.path, path_params)

        # Build headers with authentication
        headers = self._build_headers(connector, user_credentials, session_token=session_token)

        # Make request
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:  # noqa: S501 -- internal service, self-signed cert
            start_time = time.perf_counter()

            try:
                response = await client.request(
                    method=endpoint.method,
                    url=url,
                    params=query_params,
                    json=body if body else None,
                    headers=headers,
                )

                duration_ms = (time.perf_counter() - start_time) * 1000
                logger.info(
                    f"Response received in {duration_ms:.0f}ms, status: {response.status_code}"
                )

                # Parse response
                try:
                    data = response.json()
                except Exception:
                    data = response.text

                # Emit HTTP event for observability
                await self._emit_http_event(
                    method=endpoint.method,
                    url=url,
                    headers=headers,
                    request_body=body,
                    response_body=data,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                )

                if response.status_code >= 400:
                    raise UpstreamApiError(status_code=response.status_code, url=url, payload=data)

                return response.status_code, data

            except httpx.TimeoutException:
                duration_ms = (time.perf_counter() - start_time) * 1000
                # Emit timeout event
                await self._emit_http_event(
                    method=endpoint.method,
                    url=url,
                    headers=headers,
                    request_body=body,
                    response_body=None,
                    status_code=504,
                    duration_ms=duration_ms,
                    error="Request timeout",
                )
                raise UpstreamApiError(
                    status_code=504, url=url, message="Request timeout"
                ) from None
            except httpx.RequestError as e:
                duration_ms = (time.perf_counter() - start_time) * 1000
                # Emit error event
                await self._emit_http_event(
                    method=endpoint.method,
                    url=url,
                    headers=headers,
                    request_body=body,
                    response_body=None,
                    status_code=503,
                    duration_ms=duration_ms,
                    error=str(e),
                )
                raise UpstreamApiError(
                    status_code=503, url=url, message=f"Request failed: {e}"
                ) from e

    def _build_url(self, base_url: str, path: str, path_params: dict[str, Any]) -> str:
        """Build full URL with path parameter substitution."""
        # Substitute path parameters
        for param_name, param_value in path_params.items():
            path = path.replace(f"{{{param_name}}}", str(param_value))

        # Ensure no unsubstituted parameters
        if re.search(r"\{[^}]+\}", path):
            raise ValueError(f"Missing required path parameters in: {path}")

        return urljoin(base_url, path.lstrip("/"))

    def _build_headers(
        self,
        connector: Connector,
        user_credentials: dict[str, str] | None = None,
        session_token: str | None = None,
    ) -> dict[str, str]:
        """Build headers including authentication."""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        # Use user credentials if provided
        creds = user_credentials if user_credentials else connector.auth_config

        # Add authentication based on type
        if connector.auth_type == "API_KEY":
            header_name = creds.get("header_name", "X-API-Key")
            api_key = creds.get("api_key")
            if api_key:
                headers[header_name] = api_key

        elif connector.auth_type == "BASIC":
            username = creds.get("username", "")
            password = creds.get("password", "")
            credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {credentials}"

        elif connector.auth_type == "OAUTH2":
            access_token = creds.get("access_token")
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

        elif connector.auth_type == "SESSION":
            if session_token:
                session_headers = self.session_manager.build_auth_headers(connector, session_token)
                headers.update(session_headers)

        return headers
