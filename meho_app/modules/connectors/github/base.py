# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GitHub HTTP Connector Base Class.

Abstract base between BaseConnector and GitHubConnector. Manages httpx.AsyncClient
lifecycle with Bearer token (PAT) authentication, rate limit tracking from response
headers, and automatic retry on 429/403 rate limit responses.

Pattern mirrors ArgoHTTPBase but adds GitHub-specific rate limit header tracking
and handles GitHub's dual rate limit response codes (403 + 429).
"""

import asyncio
import re
from typing import Any

import httpx

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.base import BaseConnector

logger = get_logger(__name__)

# Regex for parsing GitHub Link header pagination
_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubHTTPBase(BaseConnector):
    """
    Shared base for GitHub connectors.

    Manages httpx.AsyncClient with PAT Bearer auth, rate limit tracking from
    every response, automatic retry on 429 and 403 (when rate limited), and
    Link header pagination.

    Auth: Bearer token in Authorization header (Classic PAT)
    Rate limits: Tracks x-ratelimit-remaining/limit/reset on every response
    Pagination: Link header parsing with configurable max_pages
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
        self.base_url = config.get("base_url", "https://api.github.com").rstrip("/")
        self.organization = config.get("organization", "")
        self.timeout = config.get("timeout", 30.0)

        # Rate limit budget (updated on every request)
        self._rate_limit_remaining: int | None = None
        self._rate_limit_limit: int | None = None
        self._rate_limit_reset: int | None = None

    def _build_headers(self) -> dict[str, str]:
        """
        Build HTTP headers with Bearer token auth for GitHub.

        Uses Classic PAT with repo/workflow scopes. Includes the GitHub API
        version header for stable response shapes.
        """
        token = self.credentials.get("token", "")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _update_rate_limit(self, response: httpx.Response) -> None:
        """
        Parse rate limit headers from every response.

        GitHub returns x-ratelimit-remaining, x-ratelimit-limit, and
        x-ratelimit-reset on most responses. Headers may be absent on
        some responses (e.g., redirects), so guard with None checks.
        """
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            self._rate_limit_remaining = int(remaining)
        limit = response.headers.get("x-ratelimit-limit")
        if limit is not None:
            self._rate_limit_limit = int(limit)
        reset_at = response.headers.get("x-ratelimit-reset")
        if reset_at is not None:
            self._rate_limit_reset = int(reset_at)

    def _get_rate_limit_info(self) -> dict[str, Any]:
        """Build rate limit dict for injection into every response."""
        return {
            "remaining": self._rate_limit_remaining,
            "total": self._rate_limit_limit,
            "reset_at": self._rate_limit_reset,
        }

    def _is_rate_limit_low(self) -> bool:
        """
        Check if remaining < 10% of total.

        Returns False if either value is None (rate limit not yet known).
        """
        if self._rate_limit_remaining is None or self._rate_limit_limit is None:
            return False
        return self._rate_limit_remaining < (self._rate_limit_limit * 0.1)

    async def connect(self) -> bool:
        """Create httpx.AsyncClient with Bearer auth."""
        if self._is_connected and self._client:
            return True

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._build_headers(),
            timeout=self.timeout,
        )
        self._is_connected = True
        logger.info(
            "GitHub connector connected",
            extra={
                "connector_id": self.connector_id,
                "base_url": self.base_url,
                "organization": self.organization,
            },
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

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        """
        Core request method with rate limit tracking and 429/403 retry.

        Updates rate limit state from every response. Handles both 429 (Too
        Many Requests) and 403 with x-ratelimit-remaining == 0 as rate limit
        responses (GitHub uses both). Retries once with Retry-After header
        (default 60s).
        """
        if not self._client:
            await self.connect()
        assert self._client is not None  # noqa: S101 -- runtime assertion for invariant checking

        response = await self._client.request(
            method,
            path,
            params=params,
            json=json,
            follow_redirects=follow_redirects,
        )

        # Always update rate limit from response headers
        self._update_rate_limit(response)

        # Handle rate limiting: 429 OR 403 with zero remaining
        is_rate_limited = response.status_code == 429 or (
            response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        )

        if is_rate_limited:
            retry_after = int(response.headers.get("Retry-After", "60"))
            logger.warning(
                f"Rate limited ({response.status_code}), retrying after {retry_after}s",
                extra={
                    "connector_id": self.connector_id,
                    "path": path,
                    "remaining": self._rate_limit_remaining,
                },
            )
            await asyncio.sleep(retry_after)
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                follow_redirects=follow_redirects,
            )
            self._update_rate_limit(response)

        response.raise_for_status()
        return response

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Execute GET request, return JSON response."""
        response = await self._request("GET", path, params=params)
        return response.json()

    async def _post(self, path: str, json: Any | None = None) -> dict:
        """Execute POST request, return JSON response. Handles 204 No Content."""
        response = await self._request("POST", path, json=json)
        if response.status_code == 204:
            return {}
        return response.json()

    async def _get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        """
        GET with redirect following, returning plain text.

        Used for log downloads where GitHub returns a 302 redirect to a
        CDN-served plain text file.
        """
        response = await self._request(
            "GET",
            path,
            params=params,
            follow_redirects=True,
        )
        return response.text

    async def _get_paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 5,
        per_page: int = 30,
    ) -> list[dict]:
        """
        Fetch paginated results, following Link header next URLs.

        Handles both list responses (e.g., repos) and wrapped responses
        (e.g., {"workflow_runs": [...]}). Stops at max_pages to prevent
        runaway pagination.
        """
        params = dict(params or {})
        params.setdefault("per_page", per_page)

        all_items: list[dict] = []
        url = path

        for _ in range(max_pages):
            response = await self._request("GET", url, params=params)
            data = response.json()

            if isinstance(data, list):
                all_items.extend(data)
            elif isinstance(data, dict):
                # Some endpoints wrap results in an object
                # Try common wrapper keys
                items = (
                    data.get("workflow_runs")
                    or data.get("jobs")
                    or data.get("check_runs")
                    or data.get("items")
                    or []
                )
                all_items.extend(items)

            # Check for next page via Link header
            link_header = response.headers.get("link", "")
            match = _LINK_NEXT_RE.search(link_header)
            if not match:
                break
            url = match.group(1)
            params = {}  # Next URL already contains query params

        return all_items

    def _map_http_error(self, e: Exception) -> str:
        """
        Map httpx/HTTP exceptions to OperationResult error codes.

        GitHub-specific: 403 with x-ratelimit-remaining == 0 maps to
        RATE_LIMITED (not PERMISSION_DENIED).
        """
        if isinstance(e, httpx.HTTPStatusError):
            status = e.response.status_code

            # Check if 403 is actually rate limiting
            if status == 403:
                remaining = e.response.headers.get("x-ratelimit-remaining")
                if remaining == "0":
                    return "RATE_LIMITED"
                return "PERMISSION_DENIED"

            if status == 401:
                return "AUTHENTICATION_FAILED"
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

    async def __aenter__(self) -> "GitHubHTTPBase":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.disconnect()
