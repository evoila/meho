# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Contract tests for SESSION-based authentication.

Validates API schemas and contracts for SESSION auth type.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meho_app.api.connectors import (
    TestAuthRequest,
    TestAuthResponse,
)
from meho_app.modules.connectors.schemas import (
    Connector,
    ConnectorCreate,
    UserCredentialCreate,
    UserCredentialProvide,
)


class TestConnectorSessionAuthContract:
    """Test SESSION auth type support in connector schemas"""

    def test_connector_create_supports_session_auth_type(self):
        """Verify SESSION is a valid auth_type"""
        request = ConnectorCreate(
            tenant_id="test-tenant",
            name="Test SESSION Connector",
            base_url="https://api.example.com",
            auth_type="SESSION",
            login_url="/api/v1/auth/login",
            login_method="POST",
            login_config={
                "body_template": {"username": "{{username}}", "password": "{{password}}"},
                "token_location": "header",
                "token_name": "X-Auth-Token",
                "session_duration_seconds": 3600,
            },
        )

        assert request.auth_type == "SESSION"
        assert request.login_url == "/api/v1/auth/login"
        assert request.login_method == "POST"
        assert request.login_config is not None

    def test_connector_create_session_auth_minimal(self):
        """Verify SESSION auth with minimal configuration"""
        request = ConnectorCreate(
            tenant_id="test-tenant",
            name="Test SESSION Connector",
            base_url="https://api.example.com",
            auth_type="SESSION",
            login_url="/api/v1/auth/login",
        )

        assert request.auth_type == "SESSION"
        assert request.login_url == "/api/v1/auth/login"
        assert request.login_method == "POST"  # Has default value
        assert request.login_config is None  # Optional

    def test_connector_create_session_auth_full_config(self):
        """Verify full SESSION auth configuration is accepted"""
        login_config = {
            "body_template": {
                "username": "{{username}}",
                "password": "{{password}}",
                "domain": "{{domain}}",
            },
            "token_location": "body",
            "token_name": "sessionToken",
            "token_path": "$.data.token",
            "session_duration_seconds": 7200,
        }

        request = ConnectorCreate(
            tenant_id="test-tenant",
            name="Complex SESSION Connector",
            base_url="https://complex-api.example.com",
            auth_type="SESSION",
            login_url="/auth/v2/login",
            login_method="POST",
            login_config=login_config,
        )

        assert request.login_config["body_template"]["domain"] == "{{domain}}"
        assert request.login_config["token_path"] == "$.data.token"
        assert request.login_config["session_duration_seconds"] == 7200

    def test_connector_supports_all_auth_types(self):
        """Verify all supported auth types are accepted"""
        auth_types = ["API_KEY", "BASIC", "OAUTH2", "NONE", "SESSION"]

        for auth_type in auth_types:
            request = ConnectorCreate(
                tenant_id="test-tenant",
                name=f"Test {auth_type} Connector",
                base_url="https://api.example.com",
                auth_type=auth_type,
            )
            assert request.auth_type == auth_type

    def test_connector_create_rejects_invalid_auth_type(self):
        """Verify invalid auth types are rejected"""
        with pytest.raises(ValidationError):
            ConnectorCreate(
                tenant_id="test-tenant",
                name="Invalid Connector",
                base_url="https://api.example.com",
                auth_type="INVALID_AUTH_TYPE",
            )

    def test_connector_schema_serialization(self):
        """Verify SESSION connector can be serialized/deserialized"""
        connector = Connector(
            id="test-id",
            tenant_id="test-tenant",
            name="Test SESSION Connector",
            base_url="https://api.example.com",
            auth_type="SESSION",
            auth_config={},
            credential_strategy="USER_PROVIDED",
            login_url="/api/v1/auth/login",
            login_method="POST",
            login_config={
                "body_template": {"username": "{{username}}", "password": "{{password}}"},
                "token_location": "header",
                "token_name": "X-Auth-Token",
                "session_duration_seconds": 3600,
            },
            allowed_methods=["GET", "POST"],
            blocked_methods=["DELETE"],
            default_safety_level="safe",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

        # Serialize to dict
        connector_dict = connector.model_dump()
        assert connector_dict["auth_type"] == "SESSION"
        assert connector_dict["login_url"] == "/api/v1/auth/login"

        # Deserialize from dict
        connector_restored = Connector(**connector_dict)
        assert connector_restored.auth_type == "SESSION"
        assert connector_restored.login_config == connector.login_config


class TestUserCredentialSessionContract:
    """Test SESSION credential type support in user credential schemas"""

    def test_user_credential_supports_session_type(self):
        """Verify SESSION is a valid credential_type"""
        cred = UserCredentialProvide(
            connector_id="test-connector-id",
            credential_type="SESSION",
            credentials={"username": "user", "password": "pass"},
        )

        assert cred.credential_type == "SESSION"
        assert cred.credentials["username"] == "user"

    def test_user_credential_create_supports_session(self):
        """Verify UserCredentialCreate accepts SESSION type"""
        cred = UserCredentialCreate(
            connector_id="test-connector-id",
            user_id="test-user",
            credential_type="SESSION",
            credentials={"username": "admin", "password": "secret"},
        )

        assert cred.credential_type == "SESSION"

    def test_user_credential_supports_all_types(self):
        """Verify all credential types are accepted"""
        types = ["PASSWORD", "API_KEY", "OAUTH2_TOKEN", "SESSION"]

        for cred_type in types:
            cred = UserCredentialProvide(
                connector_id="test-connector-id",
                credential_type=cred_type,
                credentials={"key": "value"},
            )
            assert cred.credential_type == cred_type

    def test_user_credential_rejects_invalid_type(self):
        """Verify invalid credential types are rejected"""
        with pytest.raises(ValidationError):
            UserCredentialProvide(
                connector_id="test-connector-id",
                credential_type="INVALID_TYPE",
                credentials={"key": "value"},
            )


class TestAuthEndpointContract:
    """Test test-auth endpoint request/response schemas"""

    def test_test_auth_request_schema(self):
        """Verify TestAuthRequest schema"""
        request = TestAuthRequest(credentials={"username": "user", "password": "pass"})

        assert request.credentials is not None
        assert request.credentials["username"] == "user"

    def test_test_auth_request_empty_credentials(self):
        """Verify TestAuthRequest with no credentials"""
        request = TestAuthRequest()

        assert request.credentials is None

    def test_test_auth_response_success_schema(self):
        """Verify TestAuthResponse schema for successful auth"""
        response = TestAuthResponse(
            success=True,
            message="Authentication successful",
            auth_type="SESSION",
            session_token_obtained=True,
            session_expires_at=datetime.now(tz=UTC),
        )

        assert response.success is True
        assert response.auth_type == "SESSION"
        assert response.session_token_obtained is True
        assert response.session_expires_at is not None
        assert response.error_detail is None

    def test_test_auth_response_failure_schema(self):
        """Verify TestAuthResponse schema for failed auth"""
        response = TestAuthResponse(
            success=False,
            message="Authentication failed",
            auth_type="SESSION",
            session_token_obtained=False,
            error_detail="Invalid credentials",
        )

        assert response.success is False
        assert response.auth_type == "SESSION"
        assert response.session_token_obtained is False
        assert response.error_detail == "Invalid credentials"
        assert response.session_expires_at is None

    def test_test_auth_response_for_basic_auth(self):
        """Verify TestAuthResponse works for BASIC auth"""
        response = TestAuthResponse(
            success=True, message="BASIC credentials configured", auth_type="BASIC"
        )

        assert response.auth_type == "BASIC"
        assert response.session_token_obtained is None  # Not applicable for BASIC

    def test_test_auth_response_serialization(self):
        """Verify TestAuthResponse can be serialized"""
        response = TestAuthResponse(
            success=True,
            message="Auth successful",
            auth_type="SESSION",
            session_token_obtained=True,
            session_expires_at=datetime.now(tz=UTC),
        )

        # Serialize to dict
        response_dict = response.model_dump()
        assert response_dict["success"] is True
        assert response_dict["auth_type"] == "SESSION"

        # Should be JSON serializable (for API responses)
        import json

        json_str = json.dumps(response_dict, default=str)
        assert "SESSION" in json_str


class TestLoginConfigContract:
    """Test login_config structure and validation"""

    def test_login_config_header_location(self):
        """Verify login_config for header-based token"""
        config = {
            "body_template": {"username": "{{username}}", "password": "{{password}}"},
            "token_location": "header",
            "token_name": "X-Auth-Token",
            "session_duration_seconds": 3600,
        }

        connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="Header Auth Connector",
            base_url="https://api.example.com",
            auth_type="SESSION",
            login_url="/login",
            login_config=config,
        )

        assert connector.login_config["token_location"] == "header"
        assert connector.login_config["token_name"] == "X-Auth-Token"

    def test_login_config_body_location(self):
        """Verify login_config for body-based token with JSONPath"""
        config = {
            "body_template": {"user": "{{username}}", "pass": "{{password}}"},
            "token_location": "body",
            "token_name": "authToken",
            "token_path": "$.data.authToken",
            "session_duration_seconds": 7200,
        }

        connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="Body Auth Connector",
            base_url="https://api.example.com",
            auth_type="SESSION",
            login_url="/authenticate",
            login_config=config,
        )

        assert connector.login_config["token_location"] == "body"
        assert connector.login_config["token_path"] == "$.data.authToken"

    def test_login_config_cookie_location(self):
        """Verify login_config for cookie-based token"""
        config = {
            "body_template": {"username": "{{username}}", "password": "{{password}}"},
            "token_location": "cookie",
            "token_name": "sessionId",
            "session_duration_seconds": 1800,
        }

        connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="Cookie Auth Connector",
            base_url="https://api.example.com",
            auth_type="SESSION",
            login_url="/login",
            login_config=config,
        )

        assert connector.login_config["token_location"] == "cookie"
        assert connector.login_config["token_name"] == "sessionId"


class TestBackwardCompatibility:
    """Ensure SESSION auth doesn't break existing auth types"""

    def test_existing_auth_types_still_work(self):
        """Verify existing connectors without SESSION fields still work"""
        # API_KEY
        api_key_connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="API Key Connector",
            base_url="https://api.example.com",
            auth_type="API_KEY",
            auth_config={"api_key": "sk-123"},
        )
        assert api_key_connector.auth_type == "API_KEY"
        assert api_key_connector.login_url is None

        # BASIC
        basic_connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="Basic Auth Connector",
            base_url="https://api.example.com",
            auth_type="BASIC",
            auth_config={"username": "user", "password": "pass"},
        )
        assert basic_connector.auth_type == "BASIC"
        assert basic_connector.login_url is None

        # OAUTH2
        oauth2_connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="OAuth2 Connector",
            base_url="https://api.example.com",
            auth_type="OAUTH2",
            auth_config={"client_id": "abc", "client_secret": "xyz"},
        )
        assert oauth2_connector.auth_type == "OAUTH2"
        assert oauth2_connector.login_url is None

        # NONE
        none_connector = ConnectorCreate(
            tenant_id="test-tenant",
            name="No Auth Connector",
            base_url="https://api.example.com",
            auth_type="NONE",
        )
        assert none_connector.auth_type == "NONE"
        assert none_connector.login_url is None

    def test_connector_without_login_config_serializes_correctly(self):
        """Verify connectors without login_config don't have null issues"""
        connector = Connector(
            id="test-id",
            tenant_id="test-tenant",
            name="API Key Connector",
            base_url="https://api.example.com",
            auth_type="API_KEY",
            auth_config={"api_key": "sk-123"},
            credential_strategy="SYSTEM",
            allowed_methods=["GET"],
            blocked_methods=[],
            default_safety_level="safe",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )

        # Serialize
        data = connector.model_dump()

        # Verify SESSION fields are present but null (except login_method which has default)
        assert "login_url" in data
        assert "login_method" in data
        assert "login_config" in data
        assert data["login_url"] is None
        assert data["login_method"] == "POST"  # Default value
        assert data["login_config"] is None
