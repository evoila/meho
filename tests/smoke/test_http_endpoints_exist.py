# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Smoke tests to verify critical HTTP endpoints exist.

These tests catch integration issues where code calls non-existent endpoints.
They run fast and should be part of critical test suite.

Why this matters:
- Mocked unit tests can pass even if endpoints don't exist
- This smoke test makes real HTTP calls to verify endpoints exist
- Catches issues like the workflow builder bug (Session 80)
"""

import httpx
import pytest

# =============================================================================
# Critical Endpoints to Test
# =============================================================================

# Format: (service_base_url, path, method, expected_status_codes)
# expected_status_codes: List of acceptable status codes
#   - 200, 201: Success
#   - 401: Auth required (endpoint exists, just needs auth)
#   - 422: Validation error (endpoint exists, just needs valid data)
#   - We treat 404 as FAILURE (endpoint doesn't exist)

CRITICAL_ENDPOINTS = [
    # =========================================================================
    # MEHO Modular Monolith - All endpoints on single service (port 8000)
    # =========================================================================
    # NOTE: As of the modular monolith refactoring, all services run as one
    # application. The separate meho-knowledge (8001), meho-openapi (8002),
    # and meho-agent (8003) services no longer exist as separate processes.
    # Main health check
    ("http://localhost:8000", "/health", "GET", [200]),
    # Readiness check (may return 503 if deps are down, but endpoint exists)
    ("http://localhost:8000", "/ready", "GET", [200, 503]),
    # Status check (requires auth -- 401 is expected without token)
    ("http://localhost:8000", "/status", "GET", [200, 401]),
    # BFF API endpoints (user-facing)
    ("http://localhost:8000", "/api/connectors/", "GET", [200, 401]),
    ("http://localhost:8000", "/api/knowledge/search", "POST", [200, 401, 422]),
    ("http://localhost:8000", "/api/knowledge/chunks", "GET", [200, 401, 422]),
    # Knowledge module health (internal)
    ("http://localhost:8000", "/knowledge/knowledge/health", "GET", [200]),
    # Agent module health (internal)
    ("http://localhost:8000", "/agent/agent/health", "GET", [200]),
    # Connectors module health (internal)
    ("http://localhost:8000", "/connectors/connectors/health", "GET", [200]),
    # Ingestion module health (internal)
    ("http://localhost:8000", "/ingestion/ingestion/health", "GET", [200]),
]


# =============================================================================
# Helper Functions
# =============================================================================


async def check_endpoint_exists(
    service_base_url: str,
    path: str,
    method: str,
    expected_status_codes: list[int],
    timeout: float = 5.0,  # noqa: ASYNC109 -- timeout parameter is part of function API
) -> tuple[bool, int, str]:
    """
    Check if an HTTP endpoint exists by making a real request.

    Args:
        service_base_url: Service base URL (e.g., http://localhost:8000)
        path: Endpoint path (e.g., /api/connectors)
        method: HTTP method (GET, POST, etc.)
        expected_status_codes: List of acceptable status codes
        timeout: Request timeout in seconds

    Returns:
        Tuple of (exists, status_code, error_message)
    """
    url = f"{service_base_url}{path}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                response = await client.get(url)
            elif method == "POST":
                # Send minimal valid JSON for POST requests
                response = await client.post(url, json={})
            elif method == "PUT":
                response = await client.put(url, json={})
            elif method == "DELETE":
                response = await client.delete(url)
            else:
                return False, 0, f"Unsupported method: {method}"

            status_code = response.status_code

            # 404 means endpoint doesn't exist - this is a failure
            if status_code == 404:
                return False, status_code, f"Endpoint not found: {method} {url}"

            # Check if status code is in expected range
            if status_code in expected_status_codes:
                return True, status_code, ""

            # Status code not in expected list, but endpoint exists
            # This might be OK (e.g., got 401 auth error instead of 200)
            # We'll consider it a pass if it's not 404
            return (
                True,
                status_code,
                f"Unexpected status {status_code} (expected {expected_status_codes}), but endpoint exists",
            )

    except httpx.ConnectError as e:
        return False, 0, f"Connection error: {e!s} - Is the service running?"
    except httpx.TimeoutException:
        return False, 0, f"Timeout after {timeout}s - Service not responding"
    except Exception as e:
        return False, 0, f"Unexpected error: {e!s}"


# =============================================================================
# Smoke Tests
# =============================================================================


class TestCriticalHTTPEndpoints:
    """
    Smoke tests for critical HTTP endpoints.

    These tests verify that endpoints our code calls actually exist.
    They make real HTTP requests (no mocking).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("service_base_url", "path", "method", "expected_status_codes"),
        CRITICAL_ENDPOINTS,
        ids=[f"{method} {path}" for _, path, method, _ in CRITICAL_ENDPOINTS],
    )
    async def test_endpoint_exists(
        self, service_base_url: str, path: str, method: str, expected_status_codes: list[int]
    ):
        """
        Test that a critical endpoint exists and responds.

        This test makes a real HTTP call. It passes if:
        - The endpoint responds (not 404)
        - Status code is in expected range, OR
        - Status code indicates endpoint exists (401, 422, etc.)

        It fails if:
        - 404 Not Found (endpoint doesn't exist)
        - Connection error (service not running)
        - Timeout (service not responding)
        """
        exists, _status_code, error_msg = await check_endpoint_exists(
            service_base_url, path, method, expected_status_codes
        )

        # Build helpful error message
        full_url = f"{service_base_url}{path}"

        assert exists, (
            f"\n❌ Critical endpoint does not exist or is unreachable!\n"
            f"   Endpoint: {method} {full_url}\n"
            f"   Error: {error_msg}\n"
            f"   \n"
            f"   This endpoint is called by our code but doesn't exist.\n"
            f"   This would cause a runtime failure in production.\n"
            f"   \n"
            f"   To fix:\n"
            f"   1. Verify the service is running: docker-compose ps\n"
            f"   2. Check if the endpoint path is correct\n"
            f"   3. Update the code to use the correct endpoint\n"
        )

    @pytest.mark.asyncio
    async def test_all_services_reachable(self):
        """
        Quick test that the modular monolith service is running and reachable.

        This is a fast sanity check before testing individual endpoints.

        NOTE: As of the modular monolith refactoring, all modules run as one
        application on port 8000. There are no separate services to check.
        """
        services = [
            ("meho (modular monolith)", "http://localhost:8000"),
        ]

        unreachable = []

        for service_name, service_base_url in services:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    # Try to hit root or health endpoint
                    try:
                        await client.get(f"{service_base_url}/health")
                    except httpx.HTTPStatusError:
                        # Health endpoint might not exist, try root
                        await client.get(service_base_url)

                    # Any response (even 404) means service is running
                    # We just want to verify connectivity

            except (httpx.ConnectError, httpx.TimeoutException):
                unreachable.append(service_name)

        assert not unreachable, (
            f"\n❌ Services not reachable: {', '.join(unreachable)}\n"
            f"   \n"
            f"   Please start services with: ./scripts/dev-env.sh up\n"
        )


# =============================================================================
# Endpoint Discovery Tests
# =============================================================================


class TestEndpointDiscovery:
    """
    Tests that discover and validate endpoints dynamically.

    These help ensure our endpoint list stays up to date.
    """

    @pytest.mark.asyncio
    async def test_bff_connectors_endpoint_accessible(self):
        """
        Specific test for the connectors endpoint that caused the bug.

        The workflow builder calls this endpoint to get available connectors.
        It MUST exist in meho-api (BFF), not meho-openapi.
        """
        url = "http://localhost:8000/api/connectors"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)

                # Should return 401 (auth required) or 200 (success)
                # Should NOT return 404 (not found)
                assert response.status_code != 404, (
                    f"Connectors endpoint not found at {url}\n"
                    f"The workflow builder needs this endpoint!\n"
                    f"Status code: {response.status_code}"
                )

        except httpx.ConnectError:
            pytest.fail(f"Cannot connect to {url} - is meho-api running?")


# =============================================================================
# Integration Health Check
# =============================================================================


class TestServiceIntegration:
    """
    Tests that verify the modular monolith modules work together.

    NOTE: As of the modular monolith refactoring, all modules run in one
    process. This test verifies that different modules are accessible
    via their respective URL prefixes.
    """

    @pytest.mark.asyncio
    async def test_modules_are_accessible(self):
        """
        Verify that all modules are accessible via their URL prefixes.

        This is a basic integration health check for the modular monolith.
        """
        # Test that knowledge module is accessible via BFF
        knowledge_accessible = await check_endpoint_exists(
            "http://localhost:8000", "/api/knowledge/chunks", "GET", [200, 401, 422]
        )

        assert knowledge_accessible[0], (
            "Knowledge module not accessible at /api/knowledge/chunks. "
            "Check that the monolith service is running."
        )


# =============================================================================
# Usage Notes
# =============================================================================

"""
Running these tests:

# Run all smoke tests including endpoint checks
pytest tests/smoke/ -v

# Run only HTTP endpoint tests
pytest tests/smoke/test_http_endpoints_exist.py -v

# Run with more detail
pytest tests/smoke/test_http_endpoints_exist.py -v -s

# Run specific endpoint test
pytest tests/smoke/test_http_endpoints_exist.py::TestCriticalHTTPEndpoints::test_endpoint_exists -v

Expected behavior:
- These tests require services to be running
- They make REAL HTTP calls (no mocking)
- Fast execution (< 5 seconds for all tests)
- Should be included in critical test suite

Adding new endpoints to test:
1. Add to CRITICAL_ENDPOINTS list at the top
2. Format: (base_url, path, method, expected_status_codes)
3. Run tests to verify

Example:
    ("http://localhost:8000", "/api/my-endpoint", "POST", [200, 422])
"""
