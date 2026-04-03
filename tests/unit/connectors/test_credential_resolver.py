# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Tests for CredentialResolver service and KeycloakUserChecker.

Covers:
- Interactive session fallback chain (user_own -> service -> fail)
- Automated session fallback chain (service -> delegated -> fail)
- Scope checking (before credential lookup)
- KeycloakUserChecker caching and fail-open behavior
- Delegation flagging and self-healing unflagging
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.connectors.credential_resolver import (
    CredentialNotFoundError,
    CredentialResolver,
    CredentialScopeError,
    CredentialSource,
    DelegationFlagCallback,
    ResolvedCredential,
    SessionType,
)
from meho_app.modules.connectors.keycloak_user_checker import KeycloakUserChecker


# ---- Fixtures ----


@pytest.fixture
def mock_cred_repo():
    """Mock CredentialRepository with configurable return values."""
    repo = AsyncMock()
    repo.get_credentials = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_keycloak_checker():
    """Mock KeycloakUserChecker with configurable return values."""
    checker = MagicMock(spec=KeycloakUserChecker)
    checker.is_user_active = MagicMock(return_value=True)
    return checker


@pytest.fixture
def mock_delegation_callback():
    """Mock DelegationFlagCallback."""
    return AsyncMock()


@pytest.fixture
def resolver(mock_cred_repo, mock_keycloak_checker, mock_delegation_callback):
    """CredentialResolver with all mocks."""
    return CredentialResolver(
        cred_repo=mock_cred_repo,
        keycloak_checker=mock_keycloak_checker,
        delegation_flag_callback=mock_delegation_callback,
    )


@pytest.fixture
def resolver_no_callback(mock_cred_repo, mock_keycloak_checker):
    """CredentialResolver without delegation flag callback."""
    return CredentialResolver(
        cred_repo=mock_cred_repo,
        keycloak_checker=mock_keycloak_checker,
    )


# ---- Test Classes ----


class TestInteractiveResolution:
    """Interactive session fallback chain: user_own -> service -> fail."""

    @pytest.mark.asyncio
    async def test_interactive_resolves_user_credential_first(self, resolver, mock_cred_repo):
        """Interactive session resolves user's own credential first."""
        user_creds = {"username": "alice", "password": "secret"}
        mock_cred_repo.get_credentials.return_value = user_creds

        result = await resolver.resolve(
            session_type=SessionType.INTERACTIVE,
            user_id="alice@example.com",
            connector_id="conn-1",
        )

        assert result.credentials == user_creds
        assert result.source == CredentialSource.USER_OWN
        assert result.delegated_by_user_id is None
        mock_cred_repo.get_credentials.assert_called_once_with("alice@example.com", "conn-1")

    @pytest.mark.asyncio
    async def test_interactive_falls_back_to_service_credential(self, resolver, mock_cred_repo):
        """Interactive session falls back to service credential when no user credential."""
        service_creds = {"username": "svc", "password": "svc-secret"}
        # First call (user cred) returns None, second call (service cred) returns credentials
        mock_cred_repo.get_credentials.side_effect = [None, service_creds]

        result = await resolver.resolve(
            session_type=SessionType.INTERACTIVE,
            user_id="alice@example.com",
            connector_id="conn-1",
        )

        assert result.credentials == service_creds
        assert result.source == CredentialSource.SERVICE
        assert mock_cred_repo.get_credentials.call_count == 2
        # Second call should be for sentinel user
        mock_cred_repo.get_credentials.assert_called_with("__service__", "conn-1")

    @pytest.mark.asyncio
    async def test_interactive_raises_not_found_when_no_credentials(self, resolver, mock_cred_repo):
        """Interactive session raises CredentialNotFoundError when neither exists."""
        mock_cred_repo.get_credentials.return_value = None

        with pytest.raises(CredentialNotFoundError) as exc_info:
            await resolver.resolve(
                session_type=SessionType.INTERACTIVE,
                user_id="alice@example.com",
                connector_id="conn-1",
            )

        assert exc_info.value.connector_id == "conn-1"
        assert exc_info.value.chain == ["user_own", "service"]


class TestAutomatedResolution:
    """Automated session fallback chain: service -> delegated -> fail."""

    @pytest.mark.asyncio
    async def test_automated_resolves_service_credential_first(self, resolver, mock_cred_repo):
        """Automated session resolves service credential first."""
        service_creds = {"username": "svc", "password": "svc-secret"}
        mock_cred_repo.get_credentials.return_value = service_creds

        result = await resolver.resolve(
            session_type=SessionType.AUTOMATED_EVENT,
            user_id="system:event",
            connector_id="conn-1",
            created_by_user_id="alice@example.com",
        )

        assert result.credentials == service_creds
        assert result.source == CredentialSource.SERVICE
        # Only one call (service cred) since it succeeded
        mock_cred_repo.get_credentials.assert_called_once_with("__service__", "conn-1")

    @pytest.mark.asyncio
    async def test_automated_falls_back_to_delegated_credential(
        self, resolver, mock_cred_repo, mock_keycloak_checker
    ):
        """Automated session falls back to creator's delegated credential."""
        delegated_creds = {"username": "alice", "password": "alice-secret"}
        # First call (service) returns None, second call (delegated) returns credentials
        mock_cred_repo.get_credentials.side_effect = [None, delegated_creds]
        mock_keycloak_checker.is_user_active.return_value = True

        result = await resolver.resolve(
            session_type=SessionType.AUTOMATED_SCHEDULER,
            user_id="system:scheduler",
            connector_id="conn-1",
            created_by_user_id="alice@example.com",
            tenant_id="test-tenant",
        )

        assert result.credentials == delegated_creds
        assert result.source == CredentialSource.DELEGATED
        assert result.delegated_by_user_id == "alice@example.com"
        mock_keycloak_checker.is_user_active.assert_called_once_with(
            "alice@example.com", "test-tenant"
        )

    @pytest.mark.asyncio
    async def test_automated_raises_not_found_when_no_credentials(
        self, resolver, mock_cred_repo, mock_keycloak_checker
    ):
        """Automated session raises CredentialNotFoundError when neither exists."""
        mock_cred_repo.get_credentials.return_value = None
        mock_keycloak_checker.is_user_active.return_value = True

        with pytest.raises(CredentialNotFoundError) as exc_info:
            await resolver.resolve(
                session_type=SessionType.AUTOMATED_EVENT,
                user_id="system:event",
                connector_id="conn-1",
                created_by_user_id="alice@example.com",
                trigger_type="event",
                trigger_id="wh-123",
                tenant_id="test-tenant",
            )

        assert exc_info.value.connector_id == "conn-1"
        assert exc_info.value.chain == ["service", "delegated"]
        assert exc_info.value.trigger_type == "event"
        assert exc_info.value.trigger_id == "wh-123"

    @pytest.mark.asyncio
    async def test_automated_skips_delegated_when_creator_inactive(
        self, resolver, mock_cred_repo, mock_keycloak_checker
    ):
        """Automated session with deactivated creator skips delegated credential."""
        mock_cred_repo.get_credentials.return_value = None  # No service credential
        mock_keycloak_checker.is_user_active.return_value = False

        with pytest.raises(CredentialNotFoundError):
            await resolver.resolve(
                session_type=SessionType.AUTOMATED_EVENT,
                user_id="system:event",
                connector_id="conn-1",
                created_by_user_id="alice@example.com",
                trigger_type="event",
                trigger_id="wh-123",
                tenant_id="test-tenant",
            )

        # Should NOT have tried to get delegated credentials
        assert mock_cred_repo.get_credentials.call_count == 1  # Only service call

    @pytest.mark.asyncio
    async def test_automated_returns_resolved_credential_with_correct_metadata(
        self, resolver, mock_cred_repo, mock_keycloak_checker
    ):
        """CredentialResolver returns ResolvedCredential with correct source and delegated_by."""
        delegated_creds = {"username": "bob", "password": "bob-secret"}
        mock_cred_repo.get_credentials.side_effect = [None, delegated_creds]
        mock_keycloak_checker.is_user_active.return_value = True

        result = await resolver.resolve(
            session_type=SessionType.AUTOMATED_EVENT,
            user_id="system:event",
            connector_id="conn-1",
            created_by_user_id="bob@example.com",
            tenant_id="test-tenant",
        )

        assert isinstance(result, ResolvedCredential)
        assert result.source == CredentialSource.DELEGATED
        assert result.delegated_by_user_id == "bob@example.com"
        assert result.credentials == delegated_creds


class TestScopeCheck:
    """Scope check happens before credential lookup."""

    @pytest.mark.asyncio
    async def test_scope_raises_error_when_connector_not_in_allowed(self, resolver, mock_cred_repo):
        """Scope check raises CredentialScopeError when connector_id not in allowed list."""
        with pytest.raises(CredentialScopeError) as exc_info:
            await resolver.resolve(
                session_type=SessionType.AUTOMATED_EVENT,
                user_id="system:event",
                connector_id="conn-3",
                allowed_connector_ids=["conn-1", "conn-2"],
            )

        assert exc_info.value.connector_id == "conn-3"
        assert exc_info.value.allowed == ["conn-1", "conn-2"]
        # No credential lookup should have occurred
        mock_cred_repo.get_credentials.assert_not_called()

    @pytest.mark.asyncio
    async def test_scope_allows_when_allowed_is_none(self, resolver, mock_cred_repo):
        """Scope check allows access when allowed_connector_ids is None (means all connectors)."""
        service_creds = {"username": "svc", "password": "svc-secret"}
        mock_cred_repo.get_credentials.return_value = service_creds

        result = await resolver.resolve(
            session_type=SessionType.INTERACTIVE,
            user_id="alice@example.com",
            connector_id="conn-1",
            allowed_connector_ids=None,
        )

        assert result.credentials == service_creds

    @pytest.mark.asyncio
    async def test_scope_allows_when_connector_in_list(self, resolver, mock_cred_repo):
        """Scope check allows access when connector_id is in allowed_connector_ids list."""
        user_creds = {"username": "alice", "password": "secret"}
        mock_cred_repo.get_credentials.return_value = user_creds

        result = await resolver.resolve(
            session_type=SessionType.INTERACTIVE,
            user_id="alice@example.com",
            connector_id="conn-2",
            allowed_connector_ids=["conn-1", "conn-2", "conn-3"],
        )

        assert result.credentials == user_creds


class TestKeycloakUserChecker:
    """KeycloakUserChecker caching and fail-open behavior."""

    def _make_checker(self):
        return KeycloakUserChecker(
            keycloak_url="http://localhost:8080",
            admin_username="admin",
            admin_password="admin",
        )

    @pytest.mark.asyncio
    def test_returns_true_for_active_user(self):
        """KeycloakUserChecker returns True for active user."""
        checker = self._make_checker()

        with patch("meho_app.modules.connectors.keycloak_user_checker.KeycloakAdmin") as MockAdmin:
            mock_admin = MagicMock()
            mock_admin.get_user_id.return_value = "kc-user-uuid"
            mock_admin.get_user.return_value = {"enabled": True}
            MockAdmin.return_value = mock_admin

            result = checker.is_user_active("alice@example.com", "test-tenant")

        assert result is True

    @pytest.mark.asyncio
    def test_returns_false_for_disabled_user(self):
        """KeycloakUserChecker returns False for disabled user."""
        checker = self._make_checker()

        with patch("meho_app.modules.connectors.keycloak_user_checker.KeycloakAdmin") as MockAdmin:
            mock_admin = MagicMock()
            mock_admin.get_user_id.return_value = "kc-user-uuid"
            mock_admin.get_user.return_value = {"enabled": False}
            MockAdmin.return_value = mock_admin

            result = checker.is_user_active("alice@example.com", "test-tenant")

        assert result is False

    @pytest.mark.asyncio
    def test_caches_result_within_ttl(self):
        """KeycloakUserChecker caches result and does not call Keycloak again within 5 minutes."""
        checker = self._make_checker()

        with patch("meho_app.modules.connectors.keycloak_user_checker.KeycloakAdmin") as MockAdmin:
            mock_admin = MagicMock()
            mock_admin.get_user_id.return_value = "kc-user-uuid"
            mock_admin.get_user.return_value = {"enabled": True}
            MockAdmin.return_value = mock_admin

            # First call
            result1 = checker.is_user_active("alice@example.com", "test-tenant")
            # Second call -- should use cache
            result2 = checker.is_user_active("alice@example.com", "test-tenant")

        assert result1 is True
        assert result2 is True
        # KeycloakAdmin should only be constructed once
        assert MockAdmin.call_count == 1

    @pytest.mark.asyncio
    def test_fails_open_when_keycloak_unreachable(self):
        """KeycloakUserChecker fails-open (returns True) when Keycloak is unreachable."""
        checker = self._make_checker()

        with patch("meho_app.modules.connectors.keycloak_user_checker.KeycloakAdmin") as MockAdmin:
            MockAdmin.side_effect = ConnectionError("Keycloak down")

            result = checker.is_user_active("alice@example.com", "test-tenant")

        assert result is True  # Fail-open


class TestDelegationFlagging:
    """Delegation flagging callback behavior."""

    @pytest.mark.asyncio
    async def test_deactivated_user_triggers_flag_callback_false(
        self, resolver, mock_cred_repo, mock_keycloak_checker, mock_delegation_callback
    ):
        """When creator is found inactive, delegation_flag_callback is called with False."""
        mock_cred_repo.get_credentials.return_value = None  # No service credential
        mock_keycloak_checker.is_user_active.return_value = False

        with pytest.raises(CredentialNotFoundError):
            await resolver.resolve(
                session_type=SessionType.AUTOMATED_EVENT,
                user_id="system:event",
                connector_id="conn-1",
                created_by_user_id="alice@example.com",
                trigger_type="event",
                trigger_id="wh-123",
                tenant_id="test-tenant",
                delegation_active=True,
            )

        mock_delegation_callback.assert_called_once_with("event", "wh-123", False)

    @pytest.mark.asyncio
    async def test_reactivated_user_triggers_unflag_callback_true(
        self, resolver, mock_cred_repo, mock_keycloak_checker, mock_delegation_callback
    ):
        """When previously-flagged user is found active, callback is called with True (auto-unflag)."""
        delegated_creds = {"username": "alice", "password": "secret"}
        mock_cred_repo.get_credentials.side_effect = [None, delegated_creds]
        mock_keycloak_checker.is_user_active.return_value = True

        result = await resolver.resolve(
            session_type=SessionType.AUTOMATED_EVENT,
            user_id="system:event",
            connector_id="conn-1",
            created_by_user_id="alice@example.com",
            trigger_type="event",
            trigger_id="wh-123",
            tenant_id="test-tenant",
            delegation_active=False,  # Previously flagged
        )

        assert result.source == CredentialSource.DELEGATED
        mock_delegation_callback.assert_called_once_with("event", "wh-123", True)

    @pytest.mark.asyncio
    async def test_active_user_with_delegation_active_true_does_not_call_callback(
        self, resolver, mock_cred_repo, mock_keycloak_checker, mock_delegation_callback
    ):
        """Already-unflagged user does not trigger callback."""
        delegated_creds = {"username": "alice", "password": "secret"}
        mock_cred_repo.get_credentials.side_effect = [None, delegated_creds]
        mock_keycloak_checker.is_user_active.return_value = True

        result = await resolver.resolve(
            session_type=SessionType.AUTOMATED_EVENT,
            user_id="system:event",
            connector_id="conn-1",
            created_by_user_id="alice@example.com",
            trigger_type="event",
            trigger_id="wh-123",
            tenant_id="test-tenant",
            delegation_active=True,  # Already unflagged
        )

        assert result.source == CredentialSource.DELEGATED
        mock_delegation_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_callback_provided_no_error(
        self, resolver_no_callback, mock_cred_repo, mock_keycloak_checker
    ):
        """Resolver without callback does not error on deactivated user path."""
        mock_cred_repo.get_credentials.return_value = None
        mock_keycloak_checker.is_user_active.return_value = False

        with pytest.raises(CredentialNotFoundError):
            await resolver_no_callback.resolve(
                session_type=SessionType.AUTOMATED_EVENT,
                user_id="system:event",
                connector_id="conn-1",
                created_by_user_id="alice@example.com",
                trigger_type="event",
                trigger_id="wh-123",
                tenant_id="test-tenant",
            )

        # Should not raise any error related to missing callback
