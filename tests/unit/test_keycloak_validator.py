# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for Keycloak JWT Validator.

Tests JWKS fetching, caching, and token validation.
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt

from meho_app.api.auth import (
    KeycloakJWTValidator,
    get_keycloak_validator,
    reset_keycloak_validator,
)
from meho_app.api.config import reset_api_config


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons before each test."""
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

    # Convert to base64url encoded integers
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

    # Get PEM format for jose
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


@pytest.fixture
def expired_token(rsa_keypair):
    """Create an expired JWT."""
    private_key, jwk = rsa_keypair

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    payload = {
        "sub": "user-123",
        "email": "test@example.com",
        "iss": "http://localhost:8080/realms/test-tenant",
        "exp": datetime.now(UTC) - timedelta(hours=1),  # Expired!
        "iat": datetime.now(UTC) - timedelta(hours=2),
        "roles": ["user"],
    }

    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": jwk["kid"]})


class TestKeycloakJWTValidator:
    """Test KeycloakJWTValidator class."""

    def test_init(self):
        """Test validator initialization."""
        validator = KeycloakJWTValidator(
            keycloak_url="http://keycloak:8080", client_id="meho-api", cache_ttl=1800
        )

        assert validator.keycloak_url == "http://keycloak:8080"
        assert validator.client_id == "meho-api"
        assert validator.cache_ttl == 1800
        assert validator._jwks_cache == {}

    def test_init_strips_trailing_slash(self):
        """Test that trailing slash is removed from URL."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080/", client_id="meho-api")

        assert validator.keycloak_url == "http://keycloak:8080"

    def test_extract_realm_from_issuer_valid(self):
        """Test realm extraction from valid issuer."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080", client_id="meho-api")

        realm = validator._extract_realm_from_issuer("http://keycloak:8080/realms/test-tenant")
        assert realm == "test-tenant"

        # Also works with localhost
        realm = validator._extract_realm_from_issuer("http://localhost:8080/realms/example-tenant")
        assert realm == "example-tenant"

        # Works with HTTPS
        realm = validator._extract_realm_from_issuer("https://auth.example.com/realms/production")
        assert realm == "production"

        # Works with master realm
        realm = validator._extract_realm_from_issuer("http://keycloak:8080/realms/master")
        assert realm == "master"

    def test_extract_realm_from_issuer_invalid(self):
        """Test realm extraction fails for invalid issuer."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080", client_id="meho-api")

        # Missing /realms/
        with pytest.raises(HTTPException) as exc_info:
            validator._extract_realm_from_issuer("http://keycloak:8080/auth")
        assert exc_info.value.status_code == 401
        assert "Invalid token issuer" in exc_info.value.detail

        # Empty realm
        with pytest.raises(HTTPException) as exc_info:
            validator._extract_realm_from_issuer("http://keycloak:8080/realms/")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_get_jwks_caches_result(self, sample_jwks):
        """Test that JWKS is cached after fetching."""
        validator = KeycloakJWTValidator(
            keycloak_url="http://keycloak:8080", client_id="meho-api", cache_ttl=3600
        )

        # Mock httpx - use MagicMock for sync methods like json()
        mock_response = MagicMock()
        mock_response.json.return_value = sample_jwks
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)

        with patch("meho_app.api.auth.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value = mock_client_instance

            # First call should fetch
            jwks1 = await validator.get_jwks("test-tenant")
            assert jwks1 == sample_jwks

            # Should be cached now
            assert "test-tenant" in validator._jwks_cache

            # Second call should use cache (no new HTTP call)
            jwks2 = await validator.get_jwks("test-tenant")
            assert jwks2 == sample_jwks

            # Only one HTTP call should have been made
            assert mock_client_instance.get.call_count == 1

    @pytest.mark.asyncio
    async def test_get_jwks_cache_expiry(self, sample_jwks):
        """Test that cached JWKS expires after TTL."""
        validator = KeycloakJWTValidator(
            keycloak_url="http://keycloak:8080",
            client_id="meho-api",
            cache_ttl=300,  # 5-minute TTL
        )

        mock_response = MagicMock()
        mock_response.json.return_value = sample_jwks
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)

        base_time = 1000000.0

        with (
            patch("meho_app.api.auth.httpx.AsyncClient") as mock_client,
            patch("meho_app.api.auth.time.time") as mock_time,
        ):
            mock_client.return_value.__aenter__.return_value = mock_client_instance

            # First call at base_time -- cache miss, fetches JWKS
            mock_time.return_value = base_time
            await validator.get_jwks("test-tenant")

            # Second call still within TTL -- should use cache
            mock_time.return_value = base_time + 100
            await validator.get_jwks("test-tenant")
            assert mock_client_instance.get.call_count == 1  # Still one HTTP call

            # Third call after TTL expires -- should fetch again
            mock_time.return_value = base_time + 301
            await validator.get_jwks("test-tenant")

            # Two HTTP calls should have been made
            assert mock_client_instance.get.call_count == 2

    @pytest.mark.asyncio
    async def test_get_jwks_unknown_realm(self):
        """Test that 404 response results in proper error."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080", client_id="meho-api")

        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_response
        )

        with patch("meho_app.api.auth.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )

            with pytest.raises(HTTPException) as exc_info:
                await validator.get_jwks("nonexistent-realm")

            assert exc_info.value.status_code == 401
            assert "Unknown realm" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_jwks_connection_error(self):
        """Test handling of connection errors to Keycloak."""
        validator = KeycloakJWTValidator(keycloak_url="http://keycloak:8080", client_id="meho-api")

        import httpx

        with patch("meho_app.api.auth.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.RequestError("Connection refused")
            )

            with pytest.raises(HTTPException) as exc_info:
                await validator.get_jwks("test-tenant")

            assert exc_info.value.status_code == 503
            assert "Cannot connect to Keycloak" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_validate_token_success(self, sample_jwks, sample_token):
        """Test successful token validation."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        # Pre-populate cache to avoid HTTP call
        validator._jwks_cache["test-tenant"] = (sample_jwks, time.time() + 3600)

        token_data = await validator.validate_token(sample_token)

        assert token_data.user_id == "test@example.com"  # Email is used as user_id
        assert token_data.tenant_id == "test-tenant"
        assert "admin" in token_data.roles
        assert "user" in token_data.roles
        assert "/team-a" in token_data.groups

    @pytest.mark.asyncio
    async def test_validate_token_expired(self, sample_jwks, expired_token):
        """Test that expired token is rejected."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        validator._jwks_cache["test-tenant"] = (sample_jwks, time.time() + 3600)

        with pytest.raises(HTTPException) as exc_info:
            await validator.validate_token(expired_token)

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_token_missing_kid(self, sample_jwks):
        """Test token without key ID is rejected."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        # Create token without kid header
        token = jwt.encode(
            {"sub": "user", "iss": "http://localhost:8080/realms/test"},
            "secret",
            algorithm="HS256",
            # No kid header
        )

        with pytest.raises(HTTPException) as exc_info:
            await validator.validate_token(token)

        assert exc_info.value.status_code == 401
        assert "kid" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_token_missing_issuer(self):
        """Test token without issuer is rejected."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        # Create token without issuer
        token = jwt.encode(
            {"sub": "user"},  # No iss claim
            "secret",
            algorithm="HS256",
            headers={"kid": "some-key"},
        )

        with pytest.raises(HTTPException) as exc_info:
            await validator.validate_token(token)

        assert exc_info.value.status_code == 401
        assert "iss" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_token_missing_subject(self, sample_jwks, rsa_keypair):
        """Test token without subject is rejected."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        validator._jwks_cache["test-tenant"] = (sample_jwks, time.time() + 3600)

        private_key, jwk = rsa_keypair
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Token without sub claim
        token = jwt.encode(
            {
                "iss": "http://localhost:8080/realms/test-tenant",
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

        with pytest.raises(HTTPException) as exc_info:
            await validator.validate_token(token)

        assert exc_info.value.status_code == 401
        assert "sub" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_token_unknown_key_refreshes_cache(self, sample_jwks, rsa_keypair):
        """Test that unknown key ID triggers cache refresh."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        # Start with empty JWKS (no matching key)
        validator._jwks_cache["test-tenant"] = ({"keys": []}, time.time() + 3600)

        mock_response = MagicMock()
        mock_response.json.return_value = sample_jwks
        mock_response.raise_for_status = MagicMock()

        mock_client_instance = AsyncMock()
        mock_client_instance.get = AsyncMock(return_value=mock_response)

        private_key, jwk = rsa_keypair
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        token = jwt.encode(
            {
                "sub": "user",
                "email": "user@test.com",
                "iss": "http://localhost:8080/realms/test-tenant",
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

        with patch("meho_app.api.auth.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value = mock_client_instance

            # Should succeed after refreshing cache
            token_data = await validator.validate_token(token)
            assert token_data.user_id == "user@test.com"

    @pytest.mark.asyncio
    async def test_validate_token_extracts_roles_from_custom_claim(self, sample_jwks, rsa_keypair):
        """Test extraction of roles from custom 'roles' claim."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        validator._jwks_cache["test-tenant"] = (sample_jwks, time.time() + 3600)

        private_key, jwk = rsa_keypair
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Token with custom 'roles' claim (our Keycloak config)
        token = jwt.encode(
            {
                "sub": "user",
                "email": "user@test.com",
                "iss": "http://localhost:8080/realms/test-tenant",
                "exp": datetime.now(UTC) + timedelta(hours=1),
                "roles": ["admin", "user", "viewer"],
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

        token_data = await validator.validate_token(token)
        assert "admin" in token_data.roles
        assert "user" in token_data.roles
        assert "viewer" in token_data.roles

    @pytest.mark.asyncio
    async def test_validate_token_extracts_roles_from_realm_access(self, sample_jwks, rsa_keypair):
        """Test fallback to realm_access for roles (standard Keycloak)."""
        validator = KeycloakJWTValidator(keycloak_url="http://localhost:8080", client_id="meho-api")

        validator._jwks_cache["test-tenant"] = (sample_jwks, time.time() + 3600)

        private_key, jwk = rsa_keypair
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        # Token with standard realm_access structure
        token = jwt.encode(
            {
                "sub": "user",
                "email": "user@test.com",
                "iss": "http://localhost:8080/realms/test-tenant",
                "exp": datetime.now(UTC) + timedelta(hours=1),
                "realm_access": {"roles": ["offline_access", "uma_authorization", "admin"]},
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": jwk["kid"]},
        )

        token_data = await validator.validate_token(token)
        assert "admin" in token_data.roles
        assert "offline_access" in token_data.roles


class TestGetKeycloakValidator:
    """Test validator singleton management."""

    def test_get_keycloak_validator_singleton(self, monkeypatch):
        """Test that get_keycloak_validator returns singleton."""
        monkeypatch.setenv("KEYCLOAK_URL", "http://test:8080")
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "test-client")
        reset_api_config()
        reset_keycloak_validator()

        validator1 = get_keycloak_validator()
        validator2 = get_keycloak_validator()

        assert validator1 is validator2
        assert validator1.keycloak_url == "http://test:8080"
        assert validator1.client_id == "test-client"

    def test_reset_keycloak_validator(self, monkeypatch):
        """Test that reset creates new instance."""
        monkeypatch.setenv("KEYCLOAK_URL", "http://test1:8080")
        reset_api_config()
        reset_keycloak_validator()

        validator1 = get_keycloak_validator()

        # Change config and reset
        monkeypatch.setenv("KEYCLOAK_URL", "http://test2:8080")
        reset_api_config()
        reset_keycloak_validator()

        validator2 = get_keycloak_validator()

        assert validator1 is not validator2
        assert validator2.keycloak_url == "http://test2:8080"
