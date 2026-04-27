# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for SessionManager Basic Auth and custom headers support (TASK-64).

Tests the vCenter-compatible SESSION auth with:
- Basic Auth for login requests
- Custom headers in login requests
- Backward compatibility with existing body-based auth
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.connectors.rest.session_manager import SessionManager
from meho_app.modules.connectors.schemas import Connector


@pytest.fixture
def vcenter_connector():
    """vCenter-style connector with Basic Auth and custom headers"""
    return Connector(
        id="conn-vcenter-1",
        tenant_id="tenant-1",
        name="vSphere vCenter",
        description="vCenter REST API",
        base_url="https://vcenter.example.com",
        auth_type="SESSION",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        login_url="/rest/com/vmware/cis/session",
        login_method="POST",
        login_config={
            "login_auth_type": "basic",  # NEW: Use Basic Auth
            "login_headers": {  # NEW: Custom headers
                "vmware-use-header-authn": "test"
            },
            "token_location": "body",
            "token_path": "$.value",
            "header_name": "vmware-api-session-id",
            "session_duration_seconds": 3600,
        },
    )


@pytest.fixture
def standard_connector():
    """Standard connector with JSON body auth (backward compatibility)"""
    return Connector(
        id="conn-standard-1",
        tenant_id="tenant-1",
        name="Standard API",
        description="Standard JSON body auth",
        base_url="https://api.example.com",
        auth_type="SESSION",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        login_url="/api/v1/auth/login",
        login_method="POST",
        login_config={
            # No login_auth_type specified - should default to "body"
            "body_template": {"username": "{{username}}", "password": "{{password}}"},
            "token_location": "header",
            "token_name": "X-Auth-Token",
            "session_duration_seconds": 7200,
        },
    )


@pytest.fixture
def user_credentials():
    """User credentials"""
    return {"username": "administrator@vsphere.local", "password": "SecurePassword123!"}


@pytest.mark.asyncio
class TestBasicAuthLogin:
    """Test Basic Auth login (vCenter pattern)"""

    async def test_login_with_basic_auth_sends_auth_header(
        self, vcenter_connector, user_credentials
    ):
        """Test that Basic Auth sends credentials in Authorization header (not body)"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "1bee8c95cc8ab1a8145b956f07db3ac2"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            result = await session_manager.login(
                connector=vcenter_connector, credentials=user_credentials
            )

            # Verify request was made with Basic Auth
            mock_client.request.assert_called_once()
            call_kwargs = mock_client.request.call_args[1]

            # Assert Basic Auth was used
            assert "auth" in call_kwargs
            assert call_kwargs["auth"] == ("administrator@vsphere.local", "SecurePassword123!")

            # Assert JSON body was NOT sent (Basic Auth doesn't use body)
            assert "json" not in call_kwargs or call_kwargs["json"] is None

            # Verify result
            session_token, _refresh_token, _expires_at, _refresh_expires_at, state = result
            assert session_token == "1bee8c95cc8ab1a8145b956f07db3ac2"
            assert state == "LOGGED_IN"

    async def test_login_with_custom_headers(self, vcenter_connector, user_credentials):
        """Test that custom login headers are sent in request"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "token123"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            await session_manager.login(connector=vcenter_connector, credentials=user_credentials)

            # Verify custom headers were sent
            call_kwargs = mock_client.request.call_args[1]
            headers = call_kwargs["headers"]

            assert "vmware-use-header-authn" in headers
            assert headers["vmware-use-header-authn"] == "test"

    async def test_basic_auth_extracts_token_from_body(self, vcenter_connector, user_credentials):
        """Test that token is correctly extracted from response body"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response (vCenter response format)
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "1bee8c95cc8ab1a8145b956f07db3ac2"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            session_token, _, _, _, _ = await session_manager.login(
                connector=vcenter_connector, credentials=user_credentials
            )

            # Verify token was extracted correctly
            assert session_token == "1bee8c95cc8ab1a8145b956f07db3ac2"

    async def test_basic_auth_with_missing_credentials(self, vcenter_connector):
        """Test Basic Auth with missing credentials"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "token"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login with partial credentials
            await session_manager.login(
                connector=vcenter_connector,
                credentials={"username": "admin"},  # Missing password
            )

            # Verify auth tuple was still passed (with empty password)
            call_kwargs = mock_client.request.call_args[1]
            assert call_kwargs["auth"] == ("admin", "")


@pytest.mark.asyncio
class TestBodyAuthBackwardCompatibility:
    """Test backward compatibility with existing body-based auth"""

    async def test_login_with_body_auth_uses_json_body(self, standard_connector, user_credentials):
        """Test that body auth sends credentials in JSON body (not Basic Auth)"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"X-Auth-Token": "token-abc123"}
            mock_response.cookies = {}
            mock_response.json.return_value = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            result = await session_manager.login(
                connector=standard_connector, credentials=user_credentials
            )

            # Verify request was made with JSON body (not Basic Auth)
            mock_client.request.assert_called_once()
            call_kwargs = mock_client.request.call_args[1]

            # Assert JSON body was used
            assert "json" in call_kwargs
            assert call_kwargs["json"] == {
                "username": "administrator@vsphere.local",
                "password": "SecurePassword123!",
            }

            # Assert Basic Auth was NOT used
            assert "auth" not in call_kwargs or call_kwargs["auth"] is None

            # Verify result
            session_token, _, _, _, _ = result
            assert session_token == "token-abc123"

    async def test_login_without_auth_type_defaults_to_body(
        self, standard_connector, user_credentials
    ):
        """Test that missing login_auth_type defaults to 'body' for backward compatibility"""
        session_manager = SessionManager()

        # Verify connector doesn't have login_auth_type
        assert "login_auth_type" not in standard_connector.login_config

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"X-Auth-Token": "token"}
            mock_response.cookies = {}
            mock_response.json.return_value = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            await session_manager.login(connector=standard_connector, credentials=user_credentials)

            # Verify JSON body was used (default behavior)
            call_kwargs = mock_client.request.call_args[1]
            assert "json" in call_kwargs
            assert call_kwargs["json"] == {
                "username": "administrator@vsphere.local",
                "password": "SecurePassword123!",
            }

    async def test_existing_connectors_still_work(self, standard_connector, user_credentials):
        """Test that existing SESSION connectors continue to work unchanged"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"X-Auth-Token": "legacy-token"}
            mock_response.cookies = {}
            mock_response.json.return_value = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login (should work exactly as before)
            session_token, _, _, _, state = await session_manager.login(
                connector=standard_connector, credentials=user_credentials
            )

            # Verify success
            assert session_token == "legacy-token"
            assert state == "LOGGED_IN"


@pytest.mark.asyncio
class TestCustomLoginHeaders:
    """Test custom login headers functionality"""

    async def test_custom_headers_merged_with_defaults(self, vcenter_connector, user_credentials):
        """Test that custom headers are merged with default headers"""
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "token"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            await session_manager.login(connector=vcenter_connector, credentials=user_credentials)

            # Verify headers include both default and custom
            call_kwargs = mock_client.request.call_args[1]
            headers = call_kwargs["headers"]

            # Default headers
            assert headers["Content-Type"] == "application/json"
            assert headers["Accept"] == "application/json"

            # Custom headers
            assert headers["vmware-use-header-authn"] == "test"

    async def test_multiple_custom_headers(self, vcenter_connector, user_credentials):
        """Test multiple custom headers"""
        # Add more custom headers
        vcenter_connector.login_config["login_headers"] = {
            "vmware-use-header-authn": "test",
            "X-Custom-Header": "value123",
            "X-Request-Id": "req-456",
        }

        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "token"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            await session_manager.login(connector=vcenter_connector, credentials=user_credentials)

            # Verify all custom headers are present
            call_kwargs = mock_client.request.call_args[1]
            headers = call_kwargs["headers"]

            assert headers["vmware-use-header-authn"] == "test"
            assert headers["X-Custom-Header"] == "value123"
            assert headers["X-Request-Id"] == "req-456"

    async def test_empty_custom_headers(self, vcenter_connector, user_credentials):
        """Test that empty custom headers dict doesn't break"""
        vcenter_connector.login_config["login_headers"] = {}

        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "token"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login (should work fine)
            session_token, _, _, _, _ = await session_manager.login(
                connector=vcenter_connector, credentials=user_credentials
            )

            assert session_token == "token"

    async def test_no_custom_headers_field(self, standard_connector, user_credentials):
        """Test connector without login_headers field (backward compatibility)"""
        # Ensure no login_headers field
        assert "login_headers" not in standard_connector.login_config

        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"X-Auth-Token": "token"}
            mock_response.cookies = {}
            mock_response.json.return_value = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login (should work with default headers only)
            session_token, _, _, _, _ = await session_manager.login(
                connector=standard_connector, credentials=user_credentials
            )

            assert session_token == "token"


@pytest.mark.asyncio
class TestRegressionPrevention:
    """Regression tests to ensure Session 61 fix doesn't break"""

    async def test_session_manager_not_affected_by_endpoint_search_fix(
        self, vcenter_connector, user_credentials
    ):
        """
        REGRESSION TEST (Session 61):
        Ensure session_manager still works after endpoint search was fixed.

        Session 61 changed endpoint search to use hybrid search instead of keyword matching.
        This test ensures session_manager (different component) still works correctly.
        """
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "session-token-123"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            session_token, _, _, _, _ = await session_manager.login(
                connector=vcenter_connector, credentials=user_credentials
            )

            # Verify session manager works independently
            assert session_token == "session-token-123"

    async def test_vcenter_auth_flow_complete(self, vcenter_connector, user_credentials):
        """
        INTEGRATION REGRESSION TEST:
        Verify complete vCenter auth flow works as expected.
        """
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Setup mock response (realistic vCenter response)
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "1bee8c95cc8ab1a8145b956f07db3ac2"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            (
                session_token,
                _refresh_token,
                expires_at,
                _refresh_expires_at,
                state,
            ) = await session_manager.login(
                connector=vcenter_connector, credentials=user_credentials
            )

            # Verify all aspects of vCenter auth
            assert session_token == "1bee8c95cc8ab1a8145b956f07db3ac2"
            assert state == "LOGGED_IN"
            assert expires_at > datetime.now(tz=UTC)

            # Verify request details
            call_kwargs = mock_client.request.call_args[1]
            assert call_kwargs["auth"] == ("administrator@vsphere.local", "SecurePassword123!")
            assert call_kwargs["headers"]["vmware-use-header-authn"] == "test"
