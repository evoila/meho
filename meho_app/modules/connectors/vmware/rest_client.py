# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Shared REST client for VMware NSX Manager and SDDC Manager APIs."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class VMwareRESTClient:
    """Reusable async HTTP client for VMware REST APIs (NSX Manager, SDDC Manager).

    Provides connection lifecycle management, automatic pagination handling
    for NSX-style cursor-based responses, and rate-limit retry logic.

    Usage::

        client = VMwareRESTClient("https://nsx.example.com", verify_ssl=False)
        client.connect({"Authorization": "Basic ..."})
        segments = await client.paginated_get("/policy/api/v1/infra/segments")
        await client.disconnect()
    """

    def __init__(self, base_url: str, verify_ssl: bool = True, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._headers: dict[str, str] = {}

    def connect(self, headers: dict[str, str]) -> None:
        """Create the underlying httpx.AsyncClient with the given headers."""
        self._headers = dict(headers)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        logger.info(f"REST client connected to {self.base_url}")

    async def disconnect(self) -> None:
        """Close the httpx client and release resources."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info(f"REST client disconnected from {self.base_url}")

    @property
    def is_connected(self) -> bool:
        """Return True if the underlying client is active."""
        return self._client is not None

    # ------------------------------------------------------------------
    # Core HTTP methods
    # ------------------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GET request with retry-on-429 and auth-error handling.

        Raises:
            RuntimeError: If the client has not been connected.
            PermissionError: On 401/403 authentication failures.
            httpx.HTTPStatusError: On other non-2xx responses.
        """
        if self._client is None:
            raise RuntimeError("REST client not connected -- call connect() first")

        response = await self._client.get(path, params=params)

        # Rate-limit handling: retry once after Retry-After delay
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            logger.warning(f"429 rate-limited on {path}, retrying after {retry_after}s")
            await asyncio.sleep(retry_after)
            response = await self._client.get(path, params=params)

        if response.status_code in (401, 403):
            raise PermissionError(
                f"Authentication failed for {path}: {response.status_code} {response.text[:200]}"
            )

        response.raise_for_status()
        return dict(response.json())

    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a POST request with retry-on-429 and auth-error handling.

        Needed for SDDC Manager token-based authentication.
        """
        if self._client is None:
            raise RuntimeError("REST client not connected -- call connect() first")

        response = await self._client.post(path, json=json)

        # Rate-limit handling: retry once after Retry-After delay
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "5"))
            logger.warning(f"429 rate-limited on POST {path}, retrying after {retry_after}s")
            await asyncio.sleep(retry_after)
            response = await self._client.post(path, json=json)

        if response.status_code in (401, 403):
            raise PermissionError(
                f"Authentication failed for POST {path}: {response.status_code} {response.text[:200]}"
            )

        response.raise_for_status()
        return dict(response.json())

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def paginated_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        page_size: int = 1000,
        results_key: str = "results",
    ) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated NSX/SDDC endpoint.

        NSX Policy API returns at most ``page_size`` results per call and
        includes a ``cursor`` field when more pages are available (Pitfall 7
        from research).  This method loops until ``cursor`` is absent.
        """
        all_results: list[dict[str, Any]] = []
        cursor: str | None = None

        while True:
            query_params: dict[str, Any] = dict(params or {})
            query_params["page_size"] = page_size
            if cursor is not None:
                query_params["cursor"] = cursor

            data = await self.get(path, params=query_params)
            all_results.extend(data.get(results_key, []))

            cursor = data.get("cursor")
            if not cursor:
                break

        return all_results

    # ------------------------------------------------------------------
    # Header management
    # ------------------------------------------------------------------

    async def update_headers(self, headers: dict[str, str]) -> None:
        """Replace client headers (e.g. after SDDC Manager token refresh).

        Closes the current client and creates a new one with updated headers.
        """
        if self._client:
            await self._client.aclose()
        self._headers = dict(headers)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        logger.info(f"REST client headers updated for {self.base_url}")
