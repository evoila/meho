# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for credential masking in connector API endpoints.

Tests that credentials are properly masked when superadmins view
tenant connector data via the X-Acting-As-Tenant header.
"""

from datetime import UTC, datetime

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.modules.connectors.schemas import Connector

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def mock_connector() -> Connector:
    """Sample connector with credentials."""
    return Connector(
        id="conn-123",
        tenant_id="tenant-1",
        name="VMware vCenter",
        description="Production vCenter",
        base_url="https://vcenter.example.com",
        connector_type="vmware",
        auth_type="SESSION",
        auth_config={
            "username": "administrator@vsphere.local",
            "password": "super-secret-password",
        },
        login_url="/api/session",
        login_method="POST",
        login_config={
            "login_auth_type": "basic",
            "token_location": "header",
            "header_name": "vmware-api-session-id",
        },
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
        credential_strategy="USER_PROVIDED",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def regular_user() -> UserContext:
    """Regular tenant user."""
    return UserContext(
        user_id="user@tenant-1.com",
        tenant_id="tenant-1",
        roles=["admin"],
        acting_as_superadmin=False,
    )


@pytest.fixture
def superadmin_in_tenant() -> UserContext:
    """Superadmin viewing tenant data."""
    return UserContext(
        user_id="superadmin@master.com",
        tenant_id="tenant-1",
        roles=["global_admin"],
        original_user_id="superadmin@master.com",
        original_tenant_id="master",
        acting_as_superadmin=True,
    )


# =============================================================================
# Test: GET Single Connector - Regular User
# =============================================================================


class TestGetConnectorRegularUser:
    """Test GET /api/connectors/{id} for regular tenant users."""

    @pytest.mark.asyncio
    async def test_regular_user_sees_credentials(
        self,
        mock_connector: Connector,
        regular_user: UserContext,
    ):
        """Regular tenant user should see all credential data."""
        from meho_app.core.credential_masker import mask_credentials

        # Simulate what the API does
        connector_data = mock_connector.model_dump()
        result = mask_credentials(connector_data, regular_user)

        # Password should NOT be masked for regular user
        assert result["auth_config"]["password"] == "super-secret-password"
        assert result.get("auth_config_masked") is None or result.get("auth_config_masked") is False


# =============================================================================
# Test: GET Single Connector - Superadmin in Tenant Context
# =============================================================================


class TestGetConnectorSuperadmin:
    """Test GET /api/connectors/{id} for superadmin in tenant context."""

    @pytest.mark.asyncio
    async def test_superadmin_sees_masked_credentials(
        self,
        mock_connector: Connector,
        superadmin_in_tenant: UserContext,
    ):
        """Superadmin should NOT see password, API keys, etc."""
        from meho_app.core.credential_masker import mask_credentials

        connector_data = mock_connector.model_dump()
        result = mask_credentials(connector_data, superadmin_in_tenant)

        # Password should be masked
        assert result["auth_config"]["password"] is None
        # Masked flag should be set
        assert result.get("auth_config_masked") is True

    @pytest.mark.asyncio
    async def test_superadmin_sees_non_sensitive_data(
        self,
        mock_connector: Connector,
        superadmin_in_tenant: UserContext,
    ):
        """Superadmin should see connector metadata."""
        from meho_app.core.credential_masker import mask_credentials

        connector_data = mock_connector.model_dump()
        result = mask_credentials(connector_data, superadmin_in_tenant)

        # Non-sensitive data preserved
        assert result["id"] == "conn-123"
        assert result["name"] == "VMware vCenter"
        assert result["base_url"] == "https://vcenter.example.com"
        assert result["connector_type"] == "vmware"
        assert result["is_active"] is True

        # Username preserved (it's not a secret by itself)
        assert result["auth_config"]["username"] == "administrator@vsphere.local"

        # Login config non-sensitive preserved
        assert result["login_config"]["header_name"] == "vmware-api-session-id"


# =============================================================================
# Test: List Connectors - Superadmin in Tenant Context
# =============================================================================


class TestListConnectorsSuperadmin:
    """Test GET /api/connectors/ for superadmin in tenant context."""

    @pytest.mark.asyncio
    async def test_list_connectors_masks_all_credentials(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """All connectors in list should have masked credentials."""
        from meho_app.core.credential_masker import mask_credentials

        # Simulate multiple connectors
        connectors = [
            {
                "id": "conn-1",
                "name": "Connector 1",
                "auth_config": {"password": "pass1", "api_key": "key1"},
            },
            {
                "id": "conn-2",
                "name": "Connector 2",
                "auth_config": {"password": "pass2", "token": "tok2"},
            },
        ]

        masked_list = [mask_credentials(c, superadmin_in_tenant) for c in connectors]

        # All passwords/keys should be masked
        assert masked_list[0]["auth_config"]["password"] is None
        assert masked_list[0]["auth_config"]["api_key"] is None
        assert masked_list[1]["auth_config"]["password"] is None
        assert masked_list[1]["auth_config"]["token"] is None


# =============================================================================
# Test: Response Schema Compatibility
# =============================================================================


class TestResponseSchemaCompatibility:
    """Test that masked responses work with ConnectorResponse schema."""

    @pytest.mark.asyncio
    async def test_masked_response_validates_with_schema(
        self,
        mock_connector: Connector,
        superadmin_in_tenant: UserContext,
    ):
        """Masked response should validate against ConnectorResponse."""
        from meho_app.api.connectors.schemas import ConnectorResponse
        from meho_app.core.credential_masker import mask_credentials

        connector_data = mock_connector.model_dump()
        masked_data = mask_credentials(connector_data, superadmin_in_tenant)

        # Should not raise validation error
        response = ConnectorResponse(**masked_data)

        assert response.id == "conn-123"
        assert response.auth_config_masked is True


# =============================================================================
# Test: Typed Connector Credentials (VMware, GCP, K8s)
# =============================================================================


class TestTypedConnectorCredentials:
    """Test masking for typed connector credentials."""

    @pytest.mark.asyncio
    async def test_gcp_service_account_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """GCP service account JSON should be masked."""
        from meho_app.core.credential_masker import mask_credentials

        connector_data = {
            "id": "gcp-1",
            "name": "GCP Production",
            "connector_type": "gcp",
            "protocol_config": {
                "project_id": "my-project",
                "service_account_json": '{"type": "service_account", "private_key": "..."}',
            },
        }

        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result["protocol_config"]["service_account_json"] is None
        assert result["protocol_config"]["project_id"] == "my-project"

    @pytest.mark.asyncio
    async def test_kubernetes_token_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Kubernetes bearer token should be masked."""
        from meho_app.core.credential_masker import mask_credentials

        connector_data = {
            "id": "k8s-1",
            "name": "K8s Cluster",
            "connector_type": "kubernetes",
            "auth_config": {
                "bearer_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
            },
            "protocol_config": {
                "server_url": "https://k8s.example.com:6443",
                "skip_tls_verification": True,
            },
        }

        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result["auth_config"]["bearer_token"] is None
        assert result["protocol_config"]["server_url"] == "https://k8s.example.com:6443"

    @pytest.mark.asyncio
    async def test_proxmox_api_token_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Proxmox API token secret should be masked."""
        from meho_app.core.credential_masker import mask_credentials

        connector_data = {
            "id": "proxmox-1",
            "name": "Proxmox Cluster",
            "connector_type": "proxmox",
            "auth_config": {
                "api_user_id": "user@pam!tokenname",  # This is like username, not a secret
                "api_token_secret": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            },
        }

        result = mask_credentials(connector_data, superadmin_in_tenant)

        # User ID preserved (it's like a username, not containing sensitive patterns)
        assert result["auth_config"]["api_user_id"] == "user@pam!tokenname"
        # Token secret masked (contains 'secret')
        assert result["auth_config"]["api_token_secret"] is None


# =============================================================================
# Test: Audit Trail Preservation
# =============================================================================


class TestAuditTrailPreservation:
    """Test that original superadmin identity is preserved for audit."""

    @pytest.mark.asyncio
    async def test_original_identity_preserved_after_masking(
        self,
        mock_connector: Connector,
        superadmin_in_tenant: UserContext,
    ):
        """Masking should not affect the user context identity for audit."""
        from meho_app.core.credential_masker import mask_credentials

        # Mask credentials
        connector_data = mock_connector.model_dump()
        mask_credentials(connector_data, superadmin_in_tenant)

        # User context should still have original identity
        assert superadmin_in_tenant.original_user_id == "superadmin@master.com"
        assert superadmin_in_tenant.original_tenant_id == "master"
        assert superadmin_in_tenant.get_audit_user_id() == "superadmin@master.com"
