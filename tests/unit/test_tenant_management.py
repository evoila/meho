# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for tenant management functionality.

TASK-139 Phase 4: Tenant Management API

Tests for:
- TenantConfigRepository tenant lifecycle methods
- KeycloakTenantManager realm management
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.agents.models import TenantAgentConfig
from meho_app.modules.agents.tenant_config_repository import TenantConfigRepository

# =============================================================================
# TenantConfigRepository Tests
# =============================================================================


class TestTenantConfigRepositoryListTenants:
    """Tests for list_all_tenants method."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def repository(self, mock_session):
        """Create a repository with mock session."""
        return TenantConfigRepository(mock_session)

    @pytest.mark.asyncio
    async def test_list_all_tenants_returns_active_only_by_default(self, repository, mock_session):
        """list_all_tenants should return only active tenants by default."""
        # Arrange
        active_tenant = TenantAgentConfig(
            tenant_id="active-tenant",
            is_active=True,
            subscription_tier="free",
        )

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [active_tenant]
        mock_session.execute.return_value = mock_result

        # Act
        tenants = await repository.list_all_tenants()

        # Assert
        assert len(tenants) == 1
        assert tenants[0].tenant_id == "active-tenant"
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_all_tenants_includes_inactive_when_requested(
        self, repository, mock_session
    ):
        """list_all_tenants should include inactive tenants when requested."""
        # Arrange
        tenants_data = [
            TenantAgentConfig(tenant_id="active", is_active=True, subscription_tier="free"),
            TenantAgentConfig(tenant_id="inactive", is_active=False, subscription_tier="free"),
        ]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = tenants_data
        mock_session.execute.return_value = mock_result

        # Act
        tenants = await repository.list_all_tenants(include_inactive=True)

        # Assert
        assert len(tenants) == 2


class TestTenantConfigRepositoryCreateTenant:
    """Tests for create_tenant method."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def repository(self, mock_session):
        """Create a repository with mock session."""
        return TenantConfigRepository(mock_session)

    @pytest.mark.asyncio
    async def test_create_tenant_with_minimal_params(self, repository, mock_session):
        """create_tenant should work with just tenant_id and display_name."""
        # Act
        result = await repository.create_tenant(
            tenant_id="new-tenant",
            display_name="New Tenant",
        )

        # Assert
        assert result.tenant_id == "new-tenant"
        assert result.display_name == "New Tenant"
        assert result.is_active is True
        assert result.subscription_tier == "free"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_tenant_with_all_params(self, repository, mock_session):
        """create_tenant should accept all optional parameters."""
        # Act
        result = await repository.create_tenant(
            tenant_id="enterprise-tenant",
            display_name="Enterprise Corp",
            subscription_tier="enterprise",
            max_connectors=100,
            max_knowledge_chunks=50000,
            max_workflows_per_day=1000,
            installation_context="Enterprise environment context",
            model_override="openai:gpt-4.1",
            temperature_override=0.7,
            features={"experimental": True},
            created_by="admin@system",
        )

        # Assert
        assert result.tenant_id == "enterprise-tenant"
        assert result.subscription_tier == "enterprise"
        assert result.max_connectors == 100
        assert result.max_knowledge_chunks == 50000
        assert result.max_workflows_per_day == 1000
        assert result.installation_context == "Enterprise environment context"
        assert result.model_override == "openai:gpt-4.1"
        assert result.temperature_override == {"value": 0.7}
        assert result.features == {"experimental": True}


class TestTenantConfigRepositoryUpdateTenant:
    """Tests for update_tenant method."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def repository(self, mock_session):
        """Create a repository with mock session."""
        return TenantConfigRepository(mock_session)

    @pytest.mark.asyncio
    async def test_update_tenant_not_found_raises_error(self, repository, mock_session):
        """update_tenant should raise ValueError if tenant not found."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(ValueError, match="Tenant 'nonexistent' not found"):
            await repository.update_tenant(
                tenant_id="nonexistent",
                display_name="Updated Name",
            )

    @pytest.mark.asyncio
    async def test_update_tenant_partial_update(self, repository, mock_session):
        """update_tenant should only update provided fields."""
        # Arrange
        existing_tenant = TenantAgentConfig(
            tenant_id="test-tenant",
            display_name="Original Name",
            subscription_tier="free",
            is_active=True,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_tenant
        mock_session.execute.return_value = mock_result

        # Act
        result = await repository.update_tenant(
            tenant_id="test-tenant",
            subscription_tier="pro",
            updated_by="admin",
        )

        # Assert
        assert result.subscription_tier == "pro"
        # display_name should remain unchanged
        assert result.display_name == "Original Name"


class TestTenantConfigRepositoryDisableEnable:
    """Tests for disable_tenant and enable_tenant methods."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def repository(self, mock_session):
        """Create a repository with mock session."""
        return TenantConfigRepository(mock_session)

    @pytest.mark.asyncio
    async def test_disable_tenant_sets_is_active_false(self, repository, mock_session):
        """disable_tenant should set is_active to False."""
        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="active-tenant",
            is_active=True,
            subscription_tier="free",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        # Act
        result = await repository.disable_tenant("active-tenant", disabled_by="admin")

        # Assert
        assert result.is_active is False
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_enable_tenant_sets_is_active_true(self, repository, mock_session):
        """enable_tenant should set is_active to True."""
        # Arrange
        tenant = TenantAgentConfig(
            tenant_id="inactive-tenant",
            is_active=False,
            subscription_tier="free",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tenant
        mock_session.execute.return_value = mock_result

        # Act
        result = await repository.enable_tenant("inactive-tenant", enabled_by="admin")

        # Assert
        assert result.is_active is True
        mock_session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_disable_tenant_not_found_raises_error(self, repository, mock_session):
        """disable_tenant should raise ValueError if tenant not found."""
        # Arrange
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        # Act & Assert
        with pytest.raises(ValueError, match="Tenant 'nonexistent' not found"):
            await repository.disable_tenant("nonexistent")


# =============================================================================
# KeycloakTenantManager Tests
# =============================================================================


class TestKeycloakTenantManager:
    """Tests for KeycloakTenantManager."""

    def test_create_realm_success(self):
        """create_realm should create realm with default roles."""
        # Patch where KeycloakAdmin is used
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_kc.return_value = mock_admin_instance

            # Import after patching
            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act
            result = manager.create_realm(
                tenant_id="new-tenant",
                display_name="New Tenant Inc",
            )

            # Assert
            assert result["realm"] == "new-tenant"
            assert result["display_name"] == "New Tenant Inc"
            assert result["enabled"] is True
            assert "admin" in result["roles_created"]
            assert "user" in result["roles_created"]
            assert "viewer" in result["roles_created"]

            mock_admin_instance.create_realm.assert_called_once()

    def test_disable_realm_calls_update(self):
        """disable_realm should update realm with enabled=False."""
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_kc.return_value = mock_admin_instance

            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act
            manager.disable_realm("test-tenant")

            # Assert
            mock_admin_instance.update_realm.assert_called_once_with(
                "test-tenant", {"enabled": False}
            )

    def test_enable_realm_calls_update(self):
        """enable_realm should update realm with enabled=True."""
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_kc.return_value = mock_admin_instance

            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act
            manager.enable_realm("test-tenant")

            # Assert
            mock_admin_instance.update_realm.assert_called_once_with(
                "test-tenant", {"enabled": True}
            )

    def test_get_realm_info_returns_dict(self):
        """get_realm_info should return realm info dict."""
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_admin_instance.get_realms.return_value = [
                {"realm": "test-tenant", "displayName": "Test", "enabled": True}
            ]
            mock_kc.return_value = mock_admin_instance

            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act
            result = manager.get_realm_info("test-tenant")

            # Assert
            assert result is not None
            assert result["realm"] == "test-tenant"
            assert result["enabled"] is True

    def test_get_realm_info_returns_none_if_not_found(self):
        """get_realm_info should return None if realm doesn't exist."""
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_admin_instance.get_realms.return_value = []
            mock_kc.return_value = mock_admin_instance

            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act
            result = manager.get_realm_info("nonexistent")

            # Assert
            assert result is None

    def test_realm_exists_returns_true(self):
        """realm_exists should return True if realm exists."""
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_admin_instance.get_realms.return_value = [
                {"realm": "existing", "displayName": "Existing", "enabled": True}
            ]
            mock_kc.return_value = mock_admin_instance

            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act & Assert
            assert manager.realm_exists("existing") is True

    def test_realm_exists_returns_false(self):
        """realm_exists should return False if realm doesn't exist."""
        with patch("meho_app.modules.agents.keycloak_admin.KeycloakAdmin") as mock_kc:
            mock_admin_instance = MagicMock()
            mock_admin_instance.get_realms.return_value = []
            mock_kc.return_value = mock_admin_instance

            from meho_app.modules.agents.keycloak_admin import KeycloakTenantManager

            manager = KeycloakTenantManager(
                server_url="http://localhost:8080",
                admin_username="admin",
                admin_password="password",
            )

            # Act & Assert
            assert manager.realm_exists("nonexistent") is False


# =============================================================================
# Tenant Schema Validation Tests
# =============================================================================


class TestCreateTenantRequestValidation:
    """Tests for CreateTenantRequest validation."""

    def test_valid_tenant_id(self):
        """Valid tenant_id should pass validation."""
        from meho_app.api.routes_tenants import CreateTenantRequest

        request = CreateTenantRequest(
            tenant_id="acme-corp",
            display_name="Acme Corporation",
        )
        assert request.tenant_id == "acme-corp"

    def test_tenant_id_lowercase_conversion(self):
        """tenant_id should be converted to lowercase."""
        from meho_app.api.routes_tenants import CreateTenantRequest

        request = CreateTenantRequest(
            tenant_id="AcMe-CoRp",
            display_name="Acme Corporation",
        )
        assert request.tenant_id == "acme-corp"

    def test_reserved_tenant_id_rejected(self):
        """Reserved tenant_id values should be rejected."""
        from pydantic import ValidationError

        from meho_app.api.routes_tenants import CreateTenantRequest

        with pytest.raises(ValidationError) as exc_info:
            CreateTenantRequest(
                tenant_id="master",
                display_name="Master Tenant",
            )
        assert "reserved" in str(exc_info.value).lower()

    def test_invalid_subscription_tier_rejected(self):
        """Invalid subscription tier should be rejected."""
        from pydantic import ValidationError

        from meho_app.api.routes_tenants import CreateTenantRequest

        with pytest.raises(ValidationError) as exc_info:
            CreateTenantRequest(
                tenant_id="test-tenant",
                display_name="Test",
                subscription_tier="invalid",
            )
        assert "subscription_tier" in str(exc_info.value).lower()

    def test_valid_subscription_tiers(self):
        """Valid subscription tiers should pass."""
        from meho_app.api.routes_tenants import CreateTenantRequest

        for tier in ["free", "pro", "enterprise"]:
            request = CreateTenantRequest(
                tenant_id="test-tenant",
                display_name="Test",
                subscription_tier=tier,
            )
            assert request.subscription_tier == tier
