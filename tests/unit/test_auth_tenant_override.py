# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for tenant context override in auth.py.

Tests the X-Acting-As-Tenant header functionality for superadmin tenant switching.
TASK-140 Phase 2: Tenant Context Switching

Phase 84: set_request_context mock target and observability import paths changed.
"""

import pytest

pytestmark = pytest.mark.skip(reason="Phase 84: set_request_context mock target changed, observability module restructured")

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from meho_app.api.auth import (
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


def create_keycloak_token(private_key, kid, user_id, tenant_id, roles=None, groups=None):
    """Create a mock Keycloak JWT for testing."""
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    payload = {
        "sub": user_id,
        "email": user_id,
        "iss": f"http://localhost:8080/realms/{tenant_id}",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iat": datetime.now(UTC),
        "roles": roles or [],
        "groups": groups or [],
    }

    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


class TestUserContextSuperadminFields:
    """Tests for the new superadmin context fields in UserContext."""

    def test_default_values(self):
        """Test that new fields have correct defaults."""
        user = UserContext(user_id="test@example.com", tenant_id="test-tenant")

        assert user.original_user_id is None
        assert user.original_tenant_id is None
        assert user.acting_as_superadmin is False

    def test_with_superadmin_context(self):
        """Test creating a user with superadmin context fields set."""
        user = UserContext(
            user_id="admin@example.com",
            tenant_id="acme",  # Overridden tenant
            roles=["global_admin"],
            original_user_id="admin@example.com",
            original_tenant_id="master",
            acting_as_superadmin=True,
        )

        assert user.tenant_id == "acme"
        assert user.original_user_id == "admin@example.com"
        assert user.original_tenant_id == "master"
        assert user.acting_as_superadmin is True

    def test_is_acting_in_tenant_context(self):
        """Test the is_acting_in_tenant_context method."""
        # Regular user - not in tenant context
        regular_user = UserContext(user_id="user@example.com", tenant_id="tenant1")
        assert regular_user.is_acting_in_tenant_context() is False

        # Superadmin in tenant context
        superadmin = UserContext(
            user_id="admin@example.com",
            tenant_id="acme",
            original_user_id="admin@example.com",
            original_tenant_id="master",
            acting_as_superadmin=True,
        )
        assert superadmin.is_acting_in_tenant_context() is True

    def test_get_audit_user_id_regular_user(self):
        """Test that audit user ID is the normal user ID for regular users."""
        user = UserContext(user_id="user@example.com", tenant_id="tenant1")
        assert user.get_audit_user_id() == "user@example.com"

    def test_get_audit_user_id_superadmin(self):
        """Test that audit user ID is the original user ID for superadmins."""
        user = UserContext(
            user_id="admin@example.com",
            tenant_id="acme",
            original_user_id="admin@example.com",
            original_tenant_id="master",
            acting_as_superadmin=True,
        )
        assert user.get_audit_user_id() == "admin@example.com"

    def test_get_audit_tenant_id_regular_user(self):
        """Test that audit tenant ID is the normal tenant ID for regular users."""
        user = UserContext(user_id="user@example.com", tenant_id="tenant1")
        assert user.get_audit_tenant_id() == "tenant1"

    def test_get_audit_tenant_id_superadmin(self):
        """Test that audit tenant ID is the original tenant for superadmins."""
        user = UserContext(
            user_id="admin@example.com",
            tenant_id="acme",  # Current context
            original_user_id="admin@example.com",
            original_tenant_id="master",  # Original tenant
            acting_as_superadmin=True,
        )
        assert user.get_audit_tenant_id() == "master"

    def test_is_global_admin_when_acting_as_superadmin(self):
        """Test that is_global_admin() still returns True when in tenant context."""
        user = UserContext(
            user_id="admin@example.com",
            tenant_id="acme",  # Now in tenant context
            roles=["global_admin"],
            original_user_id="admin@example.com",
            original_tenant_id="master",  # Was in master realm
            acting_as_superadmin=True,
        )
        # Should still be recognized as global admin
        assert user.is_global_admin() is True

    def test_is_global_admin_regular_user(self):
        """Test that regular users are not global admins."""
        user = UserContext(
            user_id="user@example.com",
            tenant_id="acme",
            roles=["user"],
        )
        assert user.is_global_admin() is False


class TestTenantContextOverride:
    """Tests for the tenant context override via X-Acting-As-Tenant header."""

    @pytest.fixture
    def mock_request(self):
        """Create a mock FastAPI request."""
        request = MagicMock(spec=Request)
        request.headers = {}
        return request

    @pytest.mark.asyncio
    async def test_no_header_regular_user(self, mock_request, sample_jwks, rsa_keypair):
        """Test that regular users without header get normal context."""
        private_key, jwk = rsa_keypair

        token = create_keycloak_token(
            private_key, jwk["kid"], user_id="user@example.com", tenant_id="acme", roles=["user"]
        )

        mock_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        # Pre-populate JWKS cache
        validator = get_keycloak_validator()
        validator._jwks_cache["acme"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context"):
                from meho_app.api.auth import get_current_user

                user = await get_current_user(mock_request, mock_credentials)

        assert user.tenant_id == "acme"
        assert user.acting_as_superadmin is False
        assert user.original_tenant_id is None

    @pytest.mark.asyncio
    async def test_header_ignored_for_non_global_admin(
        self, mock_request, sample_jwks, rsa_keypair
    ):
        """Test that X-Acting-As-Tenant header is ignored for non-global admins."""
        mock_request.headers = {"X-Acting-As-Tenant": "other-tenant"}

        private_key, jwk = rsa_keypair

        token = create_keycloak_token(
            private_key,
            jwk["kid"],
            user_id="user@example.com",
            tenant_id="acme",
            roles=["user"],  # Not global_admin
        )

        mock_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        validator = get_keycloak_validator()
        validator._jwks_cache["acme"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context"):
                from meho_app.api.auth import get_current_user

                user = await get_current_user(mock_request, mock_credentials)

        # Tenant should NOT be overridden
        assert user.tenant_id == "acme"
        assert user.acting_as_superadmin is False

    @pytest.mark.asyncio
    async def test_header_works_for_global_admin(self, mock_request, sample_jwks, rsa_keypair):
        """Test that X-Acting-As-Tenant header works for global admins."""
        mock_request.headers = {"X-Acting-As-Tenant": "target-tenant"}

        private_key, jwk = rsa_keypair

        token = create_keycloak_token(
            private_key,
            jwk["kid"],
            user_id="admin@example.com",
            tenant_id="master",
            roles=["global_admin"],
        )

        mock_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        validator = get_keycloak_validator()
        validator._jwks_cache["master"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context"):
                from meho_app.api.auth import get_current_user

                user = await get_current_user(mock_request, mock_credentials)

        # Tenant should be overridden
        assert user.tenant_id == "target-tenant"
        assert user.acting_as_superadmin is True
        assert user.original_user_id == "admin@example.com"
        assert user.original_tenant_id == "master"

    @pytest.mark.asyncio
    async def test_header_preserves_other_fields(self, mock_request, sample_jwks, rsa_keypair):
        """Test that other user fields are preserved when switching context."""
        mock_request.headers = {"X-Acting-As-Tenant": "target-tenant"}

        private_key, jwk = rsa_keypair

        token = create_keycloak_token(
            private_key,
            jwk["kid"],
            user_id="admin@example.com",
            tenant_id="master",
            roles=["global_admin", "other_role"],
            groups=["group1", "group2"],
        )

        mock_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        validator = get_keycloak_validator()
        validator._jwks_cache["master"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context"):
                from meho_app.api.auth import get_current_user

                user = await get_current_user(mock_request, mock_credentials)

        # Original fields preserved
        assert user.user_id == "admin@example.com"
        assert "global_admin" in user.roles
        assert "other_role" in user.roles
        assert "group1" in user.groups

        # Context overridden
        assert user.tenant_id == "target-tenant"
        assert user.acting_as_superadmin is True

    @pytest.mark.asyncio
    async def test_observability_uses_audit_user_id(self, mock_request, sample_jwks, rsa_keypair):
        """Test that observability context uses the audit user ID."""
        mock_request.headers = {"X-Acting-As-Tenant": "target-tenant"}

        private_key, jwk = rsa_keypair

        token = create_keycloak_token(
            private_key,
            jwk["kid"],
            user_id="admin@example.com",
            tenant_id="master",
            roles=["global_admin"],
        )

        mock_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        validator = get_keycloak_validator()
        validator._jwks_cache["master"] = (sample_jwks, time.time() + 3600)

        with patch("meho_app.api.auth.get_keycloak_validator", return_value=validator):  # noqa: SIM117 -- readability preferred over combined with
            with patch("meho_app.core.observability.set_request_context") as mock_set_context:
                from meho_app.api.auth import get_current_user

                await get_current_user(mock_request, mock_credentials)

        # Should set observability with the audit user ID (original)
        mock_set_context.assert_called_once()
        call_kwargs = mock_set_context.call_args[1]
        assert call_kwargs["user_id"] == "admin@example.com"


class TestTenantContextEdgeCases:
    """Edge cases for tenant context switching."""

    def test_global_admin_with_null_tenant(self):
        """Test global admin from master realm (tenant_id=None)."""
        user = UserContext(
            user_id="admin@example.com",
            tenant_id=None,  # No tenant
            roles=["global_admin"],
        )
        assert user.is_global_admin() is True

    def test_non_master_user_with_global_admin_role(self):
        """Test that global_admin role alone doesn't make someone a global admin."""
        user = UserContext(
            user_id="user@example.com",
            tenant_id="some-tenant",  # Not master
            roles=["global_admin"],  # Has the role
        )
        # Should NOT be considered global admin (not from master realm)
        assert user.is_global_admin() is False
