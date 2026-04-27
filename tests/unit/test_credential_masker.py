# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for credential masking utility.

Tests the mask_credentials function that hides sensitive credential
fields when superadmins view tenant connector data.
"""

import pytest

from meho_app.core.auth_context import UserContext
from meho_app.core.credential_masker import (
    is_field_sensitive,
    mask_credentials,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def regular_user() -> UserContext:
    """Regular tenant user - no masking should occur."""
    return UserContext(
        user_id="user@tenant.com",
        tenant_id="tenant-1",
        roles=["user"],
        acting_as_superadmin=False,
    )


@pytest.fixture
def superadmin_in_tenant() -> UserContext:
    """Superadmin viewing tenant data - masking should occur."""
    return UserContext(
        user_id="admin@master.com",
        tenant_id="tenant-1",
        roles=["global_admin"],
        original_user_id="admin@master.com",
        original_tenant_id="master",
        acting_as_superadmin=True,
    )


@pytest.fixture
def connector_data() -> dict:
    """Sample connector data with credentials."""
    return {
        "id": "conn-123",
        "name": "My Connector",
        "base_url": "https://api.example.com",
        "auth_type": "SESSION",
        "description": "Test connector",
        "tenant_id": "tenant-1",
        "connector_type": "rest",
        "is_active": True,
        "auth_config": {
            "username": "api-user",
            "password": "super-secret-123",
            "api_key": "sk-12345",
        },
        "login_config": {
            "login_url": "/auth/login",
            "token_path": "$.accessToken",
            "session_duration_seconds": 3600,
        },
        "protocol_config": {
            "timeout": 30,
            "verify_ssl": True,
            "service_account_json": '{"type": "service_account", "private_key": "..."}',
        },
    }


# =============================================================================
# Test: No Masking for Regular Users
# =============================================================================


class TestNoMaskingForRegularUsers:
    """Verify regular users see all credential data."""

    def test_regular_user_sees_all_data(
        self,
        regular_user: UserContext,
        connector_data: dict,
    ):
        """Regular users should see credentials unchanged."""
        result = mask_credentials(connector_data, regular_user)

        # All data should be preserved
        assert result["auth_config"]["password"] == "super-secret-123"
        assert result["auth_config"]["api_key"] == "sk-12345"
        assert "password_masked" not in result
        assert "auth_config_masked" not in result

    def test_user_without_acting_flag_sees_all_data(
        self,
        connector_data: dict,
    ):
        """User without acting_as_superadmin flag sees all data."""
        user = UserContext(
            user_id="user@example.com",
            tenant_id="tenant-1",
            roles=["admin"],
            # acting_as_superadmin defaults to False
        )

        result = mask_credentials(connector_data, user)

        assert result["auth_config"]["password"] == "super-secret-123"


# =============================================================================
# Test: Masking for Superadmins in Tenant Context
# =============================================================================


class TestMaskingForSuperadmins:
    """Verify superadmins in tenant context get masked data."""

    def test_superadmin_password_masked(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """Superadmin should not see password."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result["auth_config"]["password"] is None
        # The auth_config_masked flag should be set since password was masked inside it
        assert result.get("auth_config_masked") is True

    def test_superadmin_api_key_masked(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """Superadmin should not see API key."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result["auth_config"]["api_key"] is None

    def test_superadmin_sees_non_sensitive_data(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """Superadmin should see non-sensitive metadata."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        # Non-sensitive fields preserved
        assert result["id"] == "conn-123"
        assert result["name"] == "My Connector"
        assert result["base_url"] == "https://api.example.com"
        assert result["is_active"] is True

        # Username is in auth_config but preserved (not in SENSITIVE_FIELDS)
        assert result["auth_config"]["username"] == "api-user"

    def test_superadmin_login_config_non_sensitive_preserved(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """Superadmin should see login config metadata."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        # Login config non-sensitive fields preserved
        assert result["login_config"]["login_url"] == "/auth/login"
        assert result["login_config"]["session_duration_seconds"] == 3600


# =============================================================================
# Test: Nested Dictionary Masking
# =============================================================================


class TestNestedDictionaryMasking:
    """Verify masking works recursively on nested dicts."""

    def test_nested_auth_config_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Nested auth_config credentials should be masked."""
        data = {
            "id": "conn-1",
            "auth_config": {
                "basic": {
                    "username": "user",
                    "password": "secret",
                },
                "oauth": {
                    "access_token": "tok-123",
                    "refresh_token": "ref-456",
                },
            },
        }

        result = mask_credentials(data, superadmin_in_tenant)

        assert result["auth_config"]["basic"]["password"] is None
        assert result["auth_config"]["oauth"]["access_token"] is None
        assert result["auth_config"]["oauth"]["refresh_token"] is None
        # Username preserved (not sensitive)
        assert result["auth_config"]["basic"]["username"] == "user"

    def test_deeply_nested_credentials_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Deep nesting should still mask credentials."""
        data = {"config": {"level1": {"level2": {"secret_key": "super-deep-secret"}}}}

        result = mask_credentials(data, superadmin_in_tenant)

        # secret_key contains "key" pattern, should be masked
        assert result["config"]["level1"]["level2"]["secret_key"] is None


# =============================================================================
# Test: Service Account JSON Masking
# =============================================================================


class TestServiceAccountMasking:
    """Verify GCP service account JSON is masked."""

    def test_service_account_json_masked(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """Service account JSON should be masked for GCP connectors."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result["protocol_config"]["service_account_json"] is None

    def test_protocol_config_non_sensitive_preserved(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """Protocol config non-sensitive fields preserved."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result["protocol_config"]["timeout"] == 30
        assert result["protocol_config"]["verify_ssl"] is True


# =============================================================================
# Test: Mask Indicator Flags
# =============================================================================


class TestMaskIndicatorFlags:
    """Verify *_masked flags are set correctly."""

    def test_auth_config_masked_flag_set(
        self,
        superadmin_in_tenant: UserContext,
        connector_data: dict,
    ):
        """auth_config_masked flag should be set when auth_config has masked fields."""
        result = mask_credentials(connector_data, superadmin_in_tenant)

        assert result.get("auth_config_masked") is True

    def test_no_masked_flags_for_regular_user(
        self,
        regular_user: UserContext,
        connector_data: dict,
    ):
        """Regular users should not have *_masked flags."""
        result = mask_credentials(connector_data, regular_user)

        assert result.get("auth_config_masked") is None or result.get("auth_config_masked") is False
        assert result.get("password_masked") is None


# =============================================================================
# Test: is_field_sensitive Helper
# =============================================================================


class TestIsFieldSensitive:
    """Test the is_field_sensitive helper function."""

    @pytest.mark.parametrize(
        "field_name",
        [
            "password",
            "api_key",
            "secret",
            "token",
            "bearer_token",
            "private_key",
            "service_account_json",
            "kubeconfig",
            "access_token",
            "refresh_token",
        ],
    )
    def test_sensitive_fields_detected(self, field_name: str):
        """Known sensitive fields should be detected."""
        assert is_field_sensitive(field_name) is True

    @pytest.mark.parametrize(
        "field_name",
        [
            "user_password",  # Contains 'password'
            "API_SECRET_KEY",  # Contains 'key' and 'secret'
            "auth_token_value",  # Contains 'token'
        ],
    )
    def test_sensitive_patterns_detected(self, field_name: str):
        """Fields with sensitive patterns should be detected."""
        assert is_field_sensitive(field_name) is True

    @pytest.mark.parametrize(
        "field_name",
        [
            "name",
            "description",
            "base_url",
            "tenant_id",
            "is_active",
            "timeout",
            "username",  # Not sensitive by itself
        ],
    )
    def test_non_sensitive_fields_not_detected(self, field_name: str):
        """Non-sensitive fields should not be detected."""
        assert is_field_sensitive(field_name) is False


# =============================================================================
# Test: Edge Cases
# =============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_dict_handled(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Empty dict should return empty dict."""
        result = mask_credentials({}, superadmin_in_tenant)
        assert result == {}

    def test_none_values_not_marked_as_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Fields that are already None should be preserved as None."""
        data = {
            "auth_config": {
                "password": None,
            },
            "name": "Test",
        }

        result = mask_credentials(data, superadmin_in_tenant)

        # Password was already None, so it stays None
        assert result["auth_config"]["password"] is None
        # Name preserved
        assert result["name"] == "Test"

    def test_empty_string_values_not_marked_as_masked(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Empty strings should be preserved (nothing to hide)."""
        data = {
            "auth_config": {
                "password": "",
            },
            "name": "Test",
        }

        result = mask_credentials(data, superadmin_in_tenant)

        # Empty password preserved (nothing to hide)
        assert result["auth_config"]["password"] == ""

    def test_list_with_dicts_handled(
        self,
        superadmin_in_tenant: UserContext,
    ):
        """Lists containing dicts should have nested dicts processed."""
        data = {
            "endpoints": [
                {"path": "/api/v1", "api_key": "key-1"},
                {"path": "/api/v2", "api_key": "key-2"},
            ]
        }

        result = mask_credentials(data, superadmin_in_tenant)

        assert result["endpoints"][0]["api_key"] is None
        assert result["endpoints"][1]["api_key"] is None
        assert result["endpoints"][0]["path"] == "/api/v1"
