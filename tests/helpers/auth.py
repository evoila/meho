# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Authentication helpers for testing.

Provides utilities for obtaining Keycloak tokens in integration/E2E tests,
and mock fixtures for unit tests.

For unit tests: Use the mock_user fixture or dependency override
For integration/E2E tests: Use get_test_token() to get real Keycloak tokens
"""

from unittest.mock import AsyncMock

import httpx

from meho_app.core.auth_context import UserContext

# =============================================================================
# Keycloak Token Helpers (for integration/E2E tests)
# =============================================================================


async def get_keycloak_token(
    keycloak_url: str,
    realm: str,
    client_id: str,
    username: str,
    password: str,
) -> str:
    """
    Get a real Keycloak access token using password grant.

    Use this in integration/E2E tests that need to authenticate with Keycloak.

    Args:
        keycloak_url: Base URL of Keycloak server (e.g., "http://localhost:8080")
        realm: Keycloak realm name
        client_id: OIDC client ID
        username: User's username or email
        password: User's password

    Returns:
        JWT access token string

    Raises:
        httpx.HTTPError: If token request fails
    """
    token_url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_url,
            data={
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
            },
        )
        response.raise_for_status()
        return response.json()["access_token"]


async def get_test_token(
    username: str = "admin@test-tenant",
    password: str = "test123",  # noqa: S107 -- test fixture default, not a secret
    realm: str = "test-tenant",
    keycloak_url: str = "http://localhost:8080",
    client_id: str = "meho-frontend",
) -> str:
    """
    Get a test token from Keycloak using pre-configured test users.

    This is a convenience wrapper for integration tests using the test realm.

    Default test users (defined in tests/fixtures/keycloak/test-realm.json):
    - admin@test-tenant / test123 (roles: admin)
    - user@test-tenant / test123 (roles: user)
    - viewer@test-tenant / test123 (roles: viewer)

    For global admin tests, use realm="master" and username="superadmin".

    Args:
        username: Test user's email
        password: Test user's password
        realm: Keycloak realm name (default: test-tenant)
        keycloak_url: Keycloak server URL
        client_id: OIDC client ID

    Returns:
        JWT access token string

    Example:
        token = await get_test_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.get("/api/connectors", headers=headers)
    """
    return await get_keycloak_token(
        keycloak_url=keycloak_url,
        realm=realm,
        client_id=client_id,
        username=username,
        password=password,
    )


# =============================================================================
# Mock Fixtures (for unit tests)
# =============================================================================


def create_mock_user(
    user_id: str = "test@example.com",
    tenant_id: str = "test-tenant",
    roles: list[str] | None = None,
    groups: list[str] | None = None,
    is_global_admin: bool = False,
) -> UserContext:
    """
    Create a mock UserContext for unit tests.

    Use this when you need to mock the get_current_user dependency.

    Args:
        user_id: User's email or ID
        tenant_id: Tenant/realm ID
        roles: User's roles (default: ["user"])
        groups: User's groups (default: [])
        is_global_admin: If True, sets roles to ["global_admin"] and tenant to "master"

    Returns:
        UserContext instance

    Example:
        @pytest.fixture
        def authenticated_user():
            return create_mock_user(roles=["admin"])

        def test_something(app, authenticated_user):
            from meho_app.api.auth import get_current_user
            app.dependency_overrides[get_current_user] = lambda: authenticated_user
    """
    if is_global_admin:
        return UserContext(
            user_id=user_id,
            tenant_id="master",
            roles=["global_admin"],
            groups=groups or [],
        )

    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=roles or ["user"],
        groups=groups or [],
    )


def create_admin_user(
    user_id: str = "admin@example.com",
    tenant_id: str = "test-tenant",
) -> UserContext:
    """Create a tenant admin user context."""
    return create_mock_user(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=["admin"],
    )


def create_viewer_user(
    user_id: str = "viewer@example.com",
    tenant_id: str = "test-tenant",
) -> UserContext:
    """Create a viewer user context."""
    return create_mock_user(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=["viewer"],
    )


def create_global_admin_user(
    user_id: str = "superadmin@meho.local",
) -> UserContext:
    """Create a global admin user context."""
    return create_mock_user(
        user_id=user_id,
        is_global_admin=True,
    )


# =============================================================================
# Async Mock Helpers
# =============================================================================


def mock_get_current_user(user: UserContext):
    """
    Create an async mock for get_current_user dependency.

    Args:
        user: UserContext to return

    Returns:
        AsyncMock that returns the user

    Example:
        user = create_admin_user()
        with patch('meho_app.api.auth.get_current_user', mock_get_current_user(user)):
            response = await client.get("/api/protected")
    """
    mock = AsyncMock(return_value=user)
    return mock
