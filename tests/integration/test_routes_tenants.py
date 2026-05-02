# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for tenant management API routes.

TASK-139 Phase 4: Tenant Management API

Tests for /api/tenants endpoints with permission enforcement.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_app.api.auth import get_current_user
from meho_app.api.database import get_agent_session
from meho_app.api.routes_tenants import router
from meho_app.core.auth_context import UserContext
from meho_app.modules.agents.keycloak_admin import get_keycloak_manager

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def app():
    """Create a test FastAPI application with tenant routes."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


@pytest.fixture
def mock_session():
    """Create a mock async database session."""
    session = AsyncMock()
    return session


@pytest.fixture
def global_admin_user():
    """Create a global admin user context."""
    return UserContext(
        user_id="admin-user",
        tenant_id="master",
        roles=["global_admin"],
        groups=[],
    )


@pytest.fixture
def regular_user():
    """Create a regular user context."""
    return UserContext(
        user_id="regular-user",
        tenant_id="tenant-a",
        roles=["user"],
        groups=[],
    )


@pytest.fixture
def tenant_admin_user():
    """Create a tenant admin user context."""
    return UserContext(
        user_id="tenant-admin",
        tenant_id="tenant-a",
        roles=["admin"],
        groups=[],
    )


def create_test_client(app, user, session):
    """Create a test client with mocked dependencies."""
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_agent_session] = lambda: session
    app.dependency_overrides[get_keycloak_manager] = lambda: None
    return TestClient(app)


# =============================================================================
# Permission Enforcement Tests
# =============================================================================


class TestTenantRoutePermissions:
    """Tests for permission enforcement on tenant routes."""

    def test_list_tenants_requires_global_admin(self, app, regular_user, mock_session):
        """GET /api/tenants should require TENANT_LIST permission (global_admin)."""
        # Arrange
        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/tenants")

        # Assert
        assert response.status_code == 403
        assert "tenant:list" in response.json()["detail"].lower()

    def test_list_tenants_allowed_for_global_admin(self, app, global_admin_user, mock_session):
        """GET /api/tenants should work for global_admin."""
        # Arrange - need to set up the repository mock

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.get("/api/tenants")

        # Assert
        assert response.status_code == 200
        assert response.json()["total"] == 0

    def test_create_tenant_requires_global_admin(self, app, tenant_admin_user, mock_session):
        """POST /api/tenants should require TENANT_CREATE permission."""
        # Arrange
        client = create_test_client(app, tenant_admin_user, mock_session)

        # Act
        response = client.post(
            "/api/tenants",
            json={
                "tenant_id": "new-tenant",
                "display_name": "New Tenant",
            },
        )

        # Assert
        assert response.status_code == 403
        assert "tenant:create" in response.json()["detail"].lower()

    def test_get_tenant_requires_global_admin(self, app, regular_user, mock_session):
        """GET /api/tenants/{id} should require TENANT_LIST permission."""
        # Arrange
        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.get("/api/tenants/some-tenant")

        # Assert
        assert response.status_code == 403

    def test_update_tenant_requires_global_admin(self, app, tenant_admin_user, mock_session):
        """PATCH /api/tenants/{id} should require TENANT_UPDATE permission."""
        # Arrange
        client = create_test_client(app, tenant_admin_user, mock_session)

        # Act
        response = client.patch(
            "/api/tenants/some-tenant",
            json={"display_name": "Updated Name"},
        )

        # Assert
        assert response.status_code == 403

    def test_disable_tenant_requires_global_admin(self, app, regular_user, mock_session):
        """POST /api/tenants/{id}/disable should require TENANT_UPDATE permission."""
        # Arrange
        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.post("/api/tenants/some-tenant/disable")

        # Assert
        assert response.status_code == 403

    def test_enable_tenant_requires_global_admin(self, app, regular_user, mock_session):
        """POST /api/tenants/{id}/enable should require TENANT_UPDATE permission."""
        # Arrange
        client = create_test_client(app, regular_user, mock_session)

        # Act
        response = client.post("/api/tenants/some-tenant/enable")

        # Assert
        assert response.status_code == 403


# =============================================================================
# Tenant CRUD Operation Tests
# =============================================================================


class TestTenantListEndpoint:
    """Tests for GET /api/tenants endpoint."""

    def test_list_tenants_returns_empty_list(self, app, global_admin_user, mock_session):
        """Should return empty list when no tenants exist."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.get("/api/tenants")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["tenants"] == []
        assert data["total"] == 0

    def test_list_tenants_returns_tenants(self, app, global_admin_user, mock_session):
        """Should return list of tenants."""
        from datetime import UTC, datetime

        from meho_app.modules.agents.models import TenantAgentConfig

        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="test-tenant",
            display_name="Test Tenant",
            is_active=True,
            subscription_tier="pro",
            features={},
        )
        tenant.created_at = datetime.now(tz=UTC)
        tenant.updated_at = datetime.now(tz=UTC)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [tenant]
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.get("/api/tenants")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["tenants"][0]["tenant_id"] == "test-tenant"
        assert data["tenants"][0]["subscription_tier"] == "pro"


class TestTenantCreateEndpoint:
    """Tests for POST /api/tenants endpoint."""

    def test_create_tenant_success(self, app, global_admin_user, mock_session):
        """Should create a new tenant successfully."""

        # Arrange - mock get_config to return None (no existing tenant)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.post(
            "/api/tenants",
            json={
                "tenant_id": "new-tenant",
                "display_name": "New Tenant Inc",
                "subscription_tier": "pro",
                "create_keycloak_realm": False,
            },
        )

        # Assert
        assert response.status_code == 201
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    def test_create_tenant_conflict(self, app, global_admin_user, mock_session):
        """Should return 409 if tenant already exists."""
        from meho_app.modules.agents.models import TenantAgentConfig

        # Arrange - mock get_config to return existing tenant
        existing = TenantAgentConfig(tenant_id="existing-tenant", subscription_tier="free")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.post(
            "/api/tenants",
            json={
                "tenant_id": "existing-tenant",
                "display_name": "Existing Tenant",
            },
        )

        # Assert
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_create_tenant_invalid_id(self, app, global_admin_user, mock_session):
        """Should return 422 for invalid tenant_id format."""
        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.post(
            "/api/tenants",
            json={
                "tenant_id": "Master",  # Reserved
                "display_name": "Master Tenant",
            },
        )

        # Assert
        assert response.status_code == 422


class TestTenantGetEndpoint:
    """Tests for GET /api/tenants/{tenant_id} endpoint."""

    def test_get_tenant_success(self, app, global_admin_user, mock_session):
        """Should return tenant details."""
        from datetime import UTC, datetime

        from meho_app.modules.agents.models import TenantAgentConfig

        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="test-tenant",
            display_name="Test Tenant",
            is_active=True,
            subscription_tier="enterprise",
            max_connectors=50,
            features={"feature_a": True},
        )
        tenant.created_at = datetime.now(tz=UTC)
        tenant.updated_at = datetime.now(tz=UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.get("/api/tenants/test-tenant")

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert data["tenant_id"] == "test-tenant"
        assert data["display_name"] == "Test Tenant"
        assert data["subscription_tier"] == "enterprise"
        assert data["max_connectors"] == 50

    def test_get_tenant_not_found(self, app, global_admin_user, mock_session):
        """Should return 404 for non-existent tenant."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.get("/api/tenants/nonexistent")

        # Assert
        assert response.status_code == 404


class TestTenantUpdateEndpoint:
    """Tests for PATCH /api/tenants/{tenant_id} endpoint."""

    def test_update_tenant_success(self, app, global_admin_user, mock_session):
        """Should update tenant settings."""
        from datetime import UTC, datetime

        from meho_app.modules.agents.models import TenantAgentConfig

        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="test-tenant",
            display_name="Old Name",
            subscription_tier="free",
            is_active=True,
        )
        tenant.created_at = datetime.now(tz=UTC)
        tenant.updated_at = datetime.now(tz=UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.patch(
            "/api/tenants/test-tenant",
            json={
                "display_name": "New Name",
                "subscription_tier": "pro",
            },
        )

        # Assert
        assert response.status_code == 200
        mock_session.commit.assert_called_once()

    def test_update_tenant_not_found(self, app, global_admin_user, mock_session):
        """Should return 404 for non-existent tenant."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.patch(
            "/api/tenants/nonexistent",
            json={"display_name": "New Name"},
        )

        # Assert
        assert response.status_code == 404


class TestTenantDisableEnableEndpoints:
    """Tests for disable/enable tenant endpoints."""

    def test_disable_tenant_success(self, app, global_admin_user, mock_session):
        """Should disable a tenant."""
        from datetime import UTC, datetime

        from meho_app.modules.agents.models import TenantAgentConfig

        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="active-tenant",
            is_active=True,
            subscription_tier="free",
        )
        tenant.created_at = datetime.now(tz=UTC)
        tenant.updated_at = datetime.now(tz=UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.post("/api/tenants/active-tenant/disable")

        # Assert
        assert response.status_code == 200
        assert tenant.is_active is False
        mock_session.commit.assert_called_once()

    def test_enable_tenant_success(self, app, global_admin_user, mock_session):
        """Should enable a disabled tenant."""
        from datetime import UTC, datetime

        from meho_app.modules.agents.models import TenantAgentConfig

        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="inactive-tenant",
            is_active=False,
            subscription_tier="free",
        )
        tenant.created_at = datetime.now(tz=UTC)
        tenant.updated_at = datetime.now(tz=UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        client = create_test_client(app, global_admin_user, mock_session)

        # Act
        response = client.post("/api/tenants/inactive-tenant/enable")

        # Assert
        assert response.status_code == 200
        assert tenant.is_active is True
        mock_session.commit.assert_called_once()
