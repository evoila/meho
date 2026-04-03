# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for MEHO API authentication.

Tests Keycloak JWKS validation and user context handling.
Since test tokens are no longer supported, these tests focus on:
1. Keycloak JWT validator
2. User context creation
3. Tenant override for superadmins
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from meho_app.api.auth import (
    KeycloakJWTValidator,
    TokenData,
    get_current_user,
    get_keycloak_validator,
    reset_keycloak_validator,
)
from meho_app.api.config import reset_api_config
from meho_app.core.auth_context import UserContext


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons before and after each test."""
    reset_api_config()
    reset_keycloak_validator()
    yield
    reset_api_config()
    reset_keycloak_validator()


@pytest.fixture
def rsa_keypair():
    """Generate an RSA key pair for testing."""
    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    public_key = private_key.public_key()

    # Get public key in JWK format
    public_numbers = public_key.public_numbers()

    import base64

    def int_to_base64url(n: int, length: int) -> str:
        data = n.to_bytes(length, byteorder="big")
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "kid": "test-key-1",
        "use": "sig",
        "alg": "RS256",
        "n": int_to_base64url(public_numbers.n, 256),
        "e": int_to_base64url(public_numbers.e, 3),
    }

    return private_key, jwk


@pytest.fixture
def sample_jwks(rsa_keypair):
    """Create sample JWKS response."""
    _, jwk = rsa_keypair
    return {"keys": [jwk]}


@pytest.fixture
def sample_token(rsa_keypair):
    """Create a valid JWT signed with test RSA key."""
    private_key, jwk = rsa_keypair

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    payload = {
        "sub": "user-123",
        "email": "test@example.com",
        "preferred_username": "testuser",
        "iss": "http://localhost:8080/realms/test-tenant",
        "aud": "meho-api",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iat": datetime.now(UTC),
        "roles": ["admin", "user"],
        "groups": ["/team-a"],
    }

    token = jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": jwk["kid"]})

    return token


class TestTokenData:
    """Tests for TokenData model."""

    def test_token_data_creation(self):
        """Test TokenData can be created with required fields."""
        data = TokenData(
            user_id="user@example.com",
            tenant_id="example-tenant",
            roles=["admin"],
            groups=["/admins"],
        )

        assert data.user_id == "user@example.com"
        assert data.tenant_id == "example-tenant"
        assert "admin" in data.roles
        assert "/admins" in data.groups

    def test_token_data_defaults(self):
        """Test TokenData default values."""
        data = TokenData(user_id="user@example.com", tenant_id="example-tenant")

        assert data.roles == []
        assert data.groups == []


class TestKeycloakValidator:
    """Tests for KeycloakJWTValidator."""

    def test_validator_initialization(self):
        """Test validator initializes with correct config."""
        validator = KeycloakJWTValidator(
            keycloak_url="http://keycloak:8080", client_id="meho-api", cache_ttl=1800
        )

        assert validator.keycloak_url == "http://keycloak:8080"
        assert validator.client_id == "meho-api"
        assert validator.cache_ttl == 1800

    def test_validator_strips_trailing_slash(self):
        """Test trailing slash is removed from URL."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080/", client_id="meho-api")

        assert validator.keycloak_url == "http://keycloak:8080"

    def test_realm_extraction_valid(self):
        """Test realm extraction from valid issuer."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080", client_id="meho-api")

        realm = validator._extract_realm_from_issuer("http://keycloak:8080/realms/example-tenant")
        assert realm == "example-tenant"

        realm = validator._extract_realm_from_issuer("https://auth.example.com/realms/production")
        assert realm == "production"

    def test_realm_extraction_invalid(self):
        """Test realm extraction fails for invalid issuer."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080", client_id="meho-api")

        with pytest.raises(HTTPException) as exc_info:
            validator._extract_realm_from_issuer("http://keycloak:8080/auth")

        assert exc_info.value.status_code == 401
        assert "Invalid token issuer" in exc_info.value.detail


class TestGetKeycloakValidator:
    """Tests for validator singleton."""

    def test_returns_singleton(self, monkeypatch):
        """Test get_keycloak_validator returns same instance."""
        monkeypatch.setenv("KEYCLOAK_URL", "http://test:8080")
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test-client")
        reset_api_config()
        reset_keycloak_validator()

        validator1 = get_keycloak_validator()
        validator2 = get_keycloak_validator()

        assert validator1 is validator2

    def test_reset_creates_new_instance(self, monkeypatch):
        """Test reset_keycloak_validator clears singleton."""
        monkeypatch.setenv("KEYCLOAK_URL", "http://test1:8080")
        reset_api_config()
        reset_keycloak_validator()

        validator1 = get_keycloak_validator()

        monkeypatch.setenv("KEYCLOAK_URL", "http://test2:8080")
        reset_api_config()
        reset_keycloak_validator()

        validator2 = get_keycloak_validator()

        assert validator1 is not validator2


class TestGetCurrentUser:
    """Tests for get_current_user dependency."""

    @pytest.mark.asyncio
    async def test_returns_user_context(self, sample_jwks, sample_token):
        """Test get_current_user returns UserContext from valid token."""
        import time

        # Mock request
        mock_request = MagicMock()
        mock_request.headers = {}

        # Mock credentials
        mock_credentials = MagicMock()
        mock_credentials.credentials = sample_token

        # Pre-populate JWKS cache
        validator = get_keycloak_validator()
        validator._jwks_cache["test-tenant"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context"):
                user = await get_current_user(mock_request, mock_credentials)

        assert isinstance(user, UserContext)
        assert user.user_id == "test@example.com"
        assert user.tenant_id == "test-tenant"
        assert "admin" in user.roles

    @pytest.mark.asyncio
    async def test_superadmin_tenant_override(self, sample_jwks, rsa_keypair):
        """Test X-Acting-As-Tenant header for global admins."""
        import time

        private_key, jwk = rsa_keypair
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Create token for global admin
        admin_token = jwt.encode(
            {
                "sub": "admin-123",
                "email": "superadmin@meho.local",
                "iss": "http://localhost:8080/realms/master",
                "exp": datetime.now(UTC) + timedelta(hours=1),
                "roles": ["global_admin"],
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

        # Mock request with tenant override header
        mock_request = MagicMock()
        mock_request.headers = {"X-Acting-As-Tenant": "target-tenant"}

        mock_credentials = MagicMock()
        mock_credentials.credentials = admin_token

        # Pre-populate JWKS cache for master realm
        validator = get_keycloak_validator()
        validator._jwks_cache["master"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context"):
                user = await get_current_user(mock_request, mock_credentials)

        # User should be acting as target-tenant
        assert user.tenant_id == "target-tenant"
        assert user.acting_as_superadmin is True
        assert user.original_tenant_id == "master"
