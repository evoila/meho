"""REST connector implementing BaseConnector for REST/OpenAPI APIs.

Registered as "rest" in the connector registry. Uses httpx for async HTTP,
delegates spec parsing to openapi_parser, and supports trust tier overrides.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from meho_claude.core.connectors.auth import build_auth
from meho_claude.core.connectors.base import BaseConnector
from meho_claude.core.connectors.models import ConnectorConfig, Operation
from meho_claude.core.connectors.openapi_parser import parse_openapi_spec
from meho_claude.core.connectors.registry import register_connector

# Path parameter pattern: {paramName}
_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


@register_connector("rest")
class RESTConnector(BaseConnector):
    """REST/OpenAPI connector.

    Discovers operations from OpenAPI specs and executes HTTP requests
    with auth, path parameter substitution, and trust tier enforcement.
    """

    def __init__(self, config: ConnectorConfig, credentials: dict | None = None) -> None:
        super().__init__(config, credentials)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the httpx.AsyncClient with auth and timeout."""
        if self._client is None:
            auth = None
            if self.credentials and self.config.auth.method != "oauth2_client_credentials":
                auth = build_auth(self.config.auth, self.credentials)

            self._client = httpx.AsyncClient(
                auth=auth,
                timeout=httpx.Timeout(self.config.timeout),
            )
        return self._client

    async def test_connection(self) -> dict[str, Any]:
        """Test connectivity with a HEAD request, falling back to GET on 405.

        Returns:
            Dict with status, status_code, and response_time_ms.
        """
        client = self._get_client()
        start = time.monotonic()

        try:
            response = await client.request("HEAD", self.config.base_url)
            if response.status_code == 405:
                # HEAD not allowed, fall back to GET
                response = await client.request("GET", self.config.base_url)

            elapsed_ms = round((time.monotonic() - start) * 1000)
            return {
                "status": "ok",
                "status_code": response.status_code,
                "response_time_ms": elapsed_ms,
            }
        except Exception as exc:
            elapsed_ms = round((time.monotonic() - start) * 1000)
            return {
                "status": "error",
                "message": str(exc),
                "response_time_ms": elapsed_ms,
            }

    async def discover_operations(self) -> list[Operation]:
        """Parse OpenAPI spec into operations, applying trust overrides.

        Uses config.spec_url or config.spec_path as the spec source.
        """
        spec_source = self.config.spec_url or self.config.spec_path
        if not spec_source:
            return []

        operations = parse_openapi_spec(spec_source, self.config.name)

        # Apply trust overrides from config
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        for op in operations:
            if op.operation_id in override_map:
                op.trust_tier = override_map[op.operation_id]

        return operations

    async def execute(self, operation: Operation, params: dict[str, Any]) -> dict[str, Any]:
        """Execute an HTTP request for the given operation.

        Path parameters are substituted from params dict. Remaining params
        become query params (GET) or JSON body (POST/PUT/PATCH).

        Returns:
            Dict with status_code, headers, and data (JSON or text).
        """
        client = self._get_client()

        url_template = operation.url_template or ""
        method = (operation.http_method or "GET").upper()

        # Extract and substitute path parameters
        path_param_names = set(_PATH_PARAM_RE.findall(url_template))
        path_params = {}
        remaining_params = {}

        for key, value in params.items():
            if key in path_param_names:
                path_params[key] = value
            else:
                remaining_params[key] = value

        # Build the resolved path
        resolved_path = url_template
        for name, value in path_params.items():
            resolved_path = resolved_path.replace(f"{{{name}}}", str(value))

        # Build full URL
        base = self.config.base_url.rstrip("/")
        full_url = f"{base}{resolved_path}"

        # Build request kwargs
        kwargs: dict[str, Any] = {}
        if method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            if remaining_params:
                kwargs["params"] = remaining_params
        else:
            # POST, PUT, PATCH: send as JSON body
            if remaining_params:
                kwargs["json"] = remaining_params

        response = await client.request(method, full_url, **kwargs)

        # Parse response
        try:
            data = response.json()
        except Exception:
            data = response.text

        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "data": data,
        }

    def get_trust_tier(self, operation: Operation) -> str:
        """Determine trust tier, checking config overrides first."""
        override_map = {o.operation_id: o.trust_tier for o in self.config.trust_overrides}
        if operation.operation_id in override_map:
            return override_map[operation.operation_id]
        return operation.trust_tier

    def close(self) -> None:
        """Close the httpx client if initialized."""
        if self._client is not None:
            self._client.close()
