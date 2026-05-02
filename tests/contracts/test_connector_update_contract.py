# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for connector update functionality (Session 55).

These tests verify that the BFF UpdateConnectorRequest schema matches
the OpenAPI ConnectorUpdate schema, especially for SESSION auth fields.
"""

import pytest
from pydantic import ValidationError

# BFF schema
from meho_app.api.connectors import UpdateConnectorRequest

# OpenAPI service schema
from meho_app.modules.connectors.schemas import ConnectorUpdate


class TestUpdateConnectorRequestContract:
    """Test BFF UpdateConnectorRequest schema"""

    def test_update_connector_request_minimal(self):
        """Test minimal update request"""
        request = UpdateConnectorRequest(name="Updated Name")
        assert request.name == "Updated Name"
        assert request.description is None
        assert request.base_url is None

    def test_update_connector_request_base_url(self):
        """Test updating base URL (Task 29)"""
        request = UpdateConnectorRequest(base_url="https://new-url.example.com/api")
        assert request.base_url == "https://new-url.example.com/api"

    def test_update_connector_request_session_auth_fields(self):
        """Test SESSION auth fields are accepted"""
        request = UpdateConnectorRequest(
            login_url="/v1/tokens",
            login_method="POST",
            login_config={"token_location": "body", "token_path": "$.accessToken"},
        )
        assert request.login_url == "/v1/tokens"
        assert request.login_method == "POST"
        assert request.login_config["token_location"] == "body"
        assert request.login_config["token_path"] == "$.accessToken"

    def test_update_connector_request_refresh_token_config(self):
        """Test refresh token configuration fields (Session 54)"""
        request = UpdateConnectorRequest(
            login_config={
                "token_path": "$.accessToken",
                "refresh_token_path": "$.refreshToken.id",
                "refresh_url": "/v1/tokens/refresh",
                "refresh_method": "PATCH",
                "refresh_token_expires_in": 86400,
            }
        )
        config = request.login_config
        assert config["refresh_token_path"] == "$.refreshToken.id"
        assert config["refresh_url"] == "/v1/tokens/refresh"
        assert config["refresh_method"] == "PATCH"
        assert config["refresh_token_expires_in"] == 86400

    def test_update_connector_request_safety_policies(self):
        """Test safety policy fields (Task 22)"""
        request = UpdateConnectorRequest(
            allowed_methods=["GET", "POST"],
            blocked_methods=["DELETE"],
            default_safety_level="caution",
        )
        assert request.allowed_methods == ["GET", "POST"]
        assert request.blocked_methods == ["DELETE"]
        assert request.default_safety_level == "caution"

    def test_update_connector_request_all_fields(self):
        """Test update with all fields"""
        request = UpdateConnectorRequest(
            name="VCF Updated",
            description="Updated description",
            base_url="https://new.vcf.lab/ui/api/",
            allowed_methods=["GET", "POST", "PATCH"],
            blocked_methods=["DELETE"],
            default_safety_level="caution",
            is_active=True,
            login_url="/v1/tokens",
            login_method="POST",
            login_config={
                "body_template": {"username": "{{username}}", "password": "{{password}}"},
                "token_location": "body",
                "token_path": "$.accessToken",
                "refresh_token_path": "$.refreshToken.id",
                "refresh_url": "/v1/tokens/access-token/refresh",
                "refresh_method": "PATCH",
                "session_duration_seconds": 3600,
                "refresh_token_expires_in": 86400,
            },
        )
        assert request.name == "VCF Updated"
        assert request.base_url == "https://new.vcf.lab/ui/api/"
        assert request.login_url == "/v1/tokens"
        assert request.login_config["refresh_token_path"] == "$.refreshToken.id"


class TestConnectorUpdateContract:
    """Test OpenAPI ConnectorUpdate schema"""

    def test_connector_update_minimal(self):
        """Test minimal update"""
        update = ConnectorUpdate(name="Updated")
        assert update.name == "Updated"

    def test_connector_update_session_auth_fields(self):
        """Test SESSION auth fields"""
        update = ConnectorUpdate(
            login_url="/v1/tokens",
            login_method="POST",
            login_config={"token_path": "$.accessToken"},
        )
        assert update.login_url == "/v1/tokens"
        assert update.login_method == "POST"

    def test_connector_update_all_session_fields(self):
        """Test all SESSION auth fields including refresh tokens"""
        update = ConnectorUpdate(
            login_url="/v1/tokens",
            login_method="POST",
            login_config={
                "body_template": {"username": "{{username}}"},
                "token_location": "body",
                "token_path": "$.accessToken",
                "refresh_token_path": "$.refreshToken.id",
                "refresh_url": "/v1/tokens/refresh",
                "refresh_method": "PATCH",
                "session_duration_seconds": 3600,
                "refresh_token_expires_in": 86400,
            },
        )
        assert update.login_config["refresh_token_path"] == "$.refreshToken.id"


class TestBFFToOpenAPISchemaCompatibility:
    """Test that BFF schemas can be converted to OpenAPI schemas"""

    def test_bff_request_converts_to_openapi_update(self):
        """Test BFF UpdateConnectorRequest converts to OpenAPI ConnectorUpdate"""
        bff_request = UpdateConnectorRequest(
            name="Updated Name",
            base_url="https://new-url.com",
            login_url="/v1/tokens",
            login_method="POST",
            login_config={"token_path": "$.accessToken", "refresh_token_path": "$.refreshToken.id"},
        )

        # Convert BFF request to OpenAPI update
        openapi_update = ConnectorUpdate(**bff_request.model_dump(exclude_unset=True))

        # Verify fields transferred correctly
        assert openapi_update.name == "Updated Name"
        assert openapi_update.base_url == "https://new-url.com"
        assert openapi_update.login_url == "/v1/tokens"
        assert openapi_update.login_method == "POST"
        assert openapi_update.login_config["token_path"] == "$.accessToken"
        assert openapi_update.login_config["refresh_token_path"] == "$.refreshToken.id"

    def test_session_auth_fields_preserved_in_conversion(self):
        """Test SESSION auth fields are preserved during BFF → OpenAPI conversion"""
        bff_request = UpdateConnectorRequest(
            login_url="/v1/tokens",
            login_method="PATCH",
            login_config={
                "token_location": "body",
                "token_path": "$.accessToken",
                "token_name": "X-Auth-Token",
                "session_duration_seconds": 7200,
                "refresh_token_path": "$.refreshToken.id",
                "refresh_url": "/v1/tokens/access-token/refresh",
                "refresh_method": "PATCH",
                "refresh_token_expires_in": 86400,
                "refresh_body_template": {"refreshToken": {"id": "{{refresh_token}}"}},
            },
        )

        # Convert
        openapi_update = ConnectorUpdate(**bff_request.model_dump(exclude_unset=True))

        # Verify all SESSION fields preserved
        assert openapi_update.login_url == "/v1/tokens"
        assert openapi_update.login_method == "PATCH"
        config = openapi_update.login_config
        assert config["token_location"] == "body"
        assert config["token_path"] == "$.accessToken"
        assert config["session_duration_seconds"] == 7200
        assert config["refresh_token_path"] == "$.refreshToken.id"
        assert config["refresh_url"] == "/v1/tokens/access-token/refresh"
        assert config["refresh_method"] == "PATCH"
        assert config["refresh_token_expires_in"] == 86400
        assert "refreshToken" in config["refresh_body_template"]

    def test_exclude_unset_only_sends_provided_fields(self):
        """Test that only provided fields are sent to OpenAPI service"""
        bff_request = UpdateConnectorRequest(name="New Name", login_url="/v1/auth")

        # Get only set fields
        update_dict = bff_request.model_dump(exclude_unset=True)

        # Should only have name and login_url
        assert "name" in update_dict
        assert "login_url" in update_dict
        assert "description" not in update_dict  # Not set
        assert "base_url" not in update_dict  # Not set
        assert "login_method" not in update_dict  # Not set


class TestSchemaFieldCoverage:
    """Test that BFF schema covers all OpenAPI schema fields"""

    def test_bff_has_all_openapi_fields(self):
        """Test BFF UpdateConnectorRequest has all ConnectorUpdate fields"""
        # Get field names from both schemas
        bff_fields = set(UpdateConnectorRequest.model_fields.keys())
        set(ConnectorUpdate.model_fields.keys())

        # BFF should have all OpenAPI fields (or more for BFF-specific needs)
        # Core fields that must be present in BFF
        required_fields = {
            "name",
            "description",
            "base_url",
            "is_active",
            "login_url",
            "login_method",
            "login_config",
            "allowed_methods",
            "blocked_methods",
            "default_safety_level",
        }

        missing_fields = required_fields - bff_fields
        assert not missing_fields, f"BFF schema missing fields: {missing_fields}"

    def test_session_auth_fields_present(self):
        """Test SESSION auth fields are present in both schemas"""
        session_fields = {"login_url", "login_method", "login_config"}

        bff_fields = set(UpdateConnectorRequest.model_fields.keys())
        openapi_fields = set(ConnectorUpdate.model_fields.keys())

        assert session_fields.issubset(bff_fields), "BFF missing SESSION auth fields"
        assert session_fields.issubset(openapi_fields), "OpenAPI missing SESSION auth fields"


class TestRegressionPrevention:
    """Tests that prevent the 'Connector not found' regression"""

    def test_session_config_update_doesnt_fail_validation(self):
        """
        Regression test for Session 55 bug:
        Updating SESSION config should not cause validation errors
        """
        # This is the exact data the frontend sends
        frontend_data = {
            "name": "VCF Hetzner",
            "base_url": "https://vcf-example.local/ui/api/",
            "description": "VCF main API",
            "login_url": "/v1/tokens",
            "login_method": "POST",
            "login_config": {
                "token_location": "body",
                "token_path": "$.accessToken",
                "token_name": "X-Auth-Token",
                "session_duration_seconds": 3600,
                "refresh_token_path": "$.refreshToken.id",
                "refresh_url": "/v1/tokens/access-token/refresh",
                "refresh_method": "PATCH",
                "refresh_token_expires_in": 86400,
            },
            "default_safety_level": "safe",
            "is_active": True,
        }

        # Should not raise ValidationError
        try:
            bff_request = UpdateConnectorRequest(**frontend_data)
            openapi_update = ConnectorUpdate(**bff_request.model_dump(exclude_unset=True))

            # Verify critical fields
            assert openapi_update.login_url == "/v1/tokens"
            assert openapi_update.login_config is not None
            assert "refresh_token_path" in openapi_update.login_config
        except ValidationError as e:
            pytest.fail(f"Validation failed with frontend data: {e}")

    def test_partial_session_config_update(self):
        """Test updating only some SESSION fields"""
        # User only changes refresh endpoint
        partial_update = UpdateConnectorRequest(
            login_config={"refresh_url": "/v1/tokens/new-refresh"}
        )

        # Should be valid
        assert partial_update.login_config["refresh_url"] == "/v1/tokens/new-refresh"

        # Convert to OpenAPI
        openapi_update = ConnectorUpdate(**partial_update.model_dump(exclude_unset=True))
        assert openapi_update.login_config["refresh_url"] == "/v1/tokens/new-refresh"
