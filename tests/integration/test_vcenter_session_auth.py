# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for vCenter SESSION auth flow (TASK-64).

Tests the complete authentication flow with:
- Basic Auth for login
- Custom headers
- Token extraction and usage
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meho_app.modules.connectors.rest.session_manager import SessionManager
from meho_app.modules.connectors.schemas import Connector


@pytest.fixture
def vcenter_connector():
    """Complete vCenter connector configuration"""
    return Connector(
        id="conn-vcenter-test",
        tenant_id="test-tenant",
        name="vSphere vCenter Integration Test",
        description="vCenter 8.0 REST API",
        base_url="https://vcenter-test.example.com",
        auth_type="SESSION",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        login_url="/rest/com/vmware/cis/session",
        login_method="POST",
        login_config={
            "login_auth_type": "basic",
            "login_headers": {"vmware-use-header-authn": "test"},
            "token_location": "body",
            "token_path": "$.value",
            "header_name": "vmware-api-session-id",
            "session_duration_seconds": 3600,
        },
        allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        blocked_methods=[],
        default_safety_level="safe",
    )


@pytest.fixture
def vcenter_credentials():
    """vCenter admin credentials"""
    return {"username": "administrator@vsphere.local", "password": "VMware123!"}


@pytest.mark.asyncio
class TestVCenterAuthFlow:
    """Integration tests for complete vCenter authentication flow"""

    async def test_complete_vcenter_login_flow(self, vcenter_connector, vcenter_credentials):
        """
        Test complete vCenter login flow:
        1. Send POST with Basic Auth + custom headers
        2. Extract token from response body ($.value)
        3. Return session token and expiry
        """
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Mock vCenter login response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "value": "1bee8c95cc8ab1a8145b956f07db3ac2"  # vCenter session token format
            }
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
                connector=vcenter_connector, credentials=vcenter_credentials
            )

            # Verify login request
            mock_client.request.assert_called_once()
            call_args = mock_client.request.call_args
            call_kwargs = call_args[1]

            # Step 1: Verify Basic Auth
            assert "auth" in call_kwargs
            assert call_kwargs["auth"] == ("administrator@vsphere.local", "VMware123!")

            # Step 2: Verify custom headers
            headers = call_kwargs["headers"]
            assert "vmware-use-header-authn" in headers
            assert headers["vmware-use-header-authn"] == "test"

            # Step 3: Verify method and URL
            assert call_kwargs["method"] == "POST"
            assert "/rest/com/vmware/cis/session" in call_kwargs["url"]

            # Step 4: Verify token extraction
            assert session_token == "1bee8c95cc8ab1a8145b956f07db3ac2"
            assert state == "LOGGED_IN"
            assert expires_at > datetime.now(tz=UTC)

    async def test_vcenter_api_call_with_session_token(self, vcenter_connector):
        """
        Test API call after login uses session token in custom header.
        """
        session_manager = SessionManager()
        session_token = "1bee8c95cc8ab1a8145b956f07db3ac2"

        # Build auth headers
        auth_headers = session_manager.build_auth_headers(vcenter_connector, session_token)

        # Verify custom header is used (not Bearer)
        assert "vmware-api-session-id" in auth_headers
        assert auth_headers["vmware-api-session-id"] == session_token

        # Verify Bearer is NOT used (vCenter doesn't use it)
        assert "Authorization" not in auth_headers

    async def test_vcenter_token_reuse_within_validity(
        self, vcenter_connector, vcenter_credentials
    ):
        """
        Test that valid session tokens are reused (not re-login every time).
        """
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Mock login response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"value": "token-abc123"}
            mock_response.headers = {}
            mock_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # First login
            token1, _, expires1, _, _ = await session_manager.login(
                connector=vcenter_connector, credentials=vcenter_credentials
            )

            # Second login with still-valid token
            token2, _, expires2, _, _ = await session_manager.login(
                connector=vcenter_connector,
                credentials=vcenter_credentials,
                session_token=token1,
                session_expires_at=expires1,
            )

            # Verify token was reused (no second HTTP request)
            assert mock_client.request.call_count == 1  # Only one login
            assert token1 == token2
            assert expires1 == expires2

    async def test_vcenter_login_failure_handling(self, vcenter_connector, vcenter_credentials):
        """
        Test handling of vCenter login failures (401 Unauthorized).
        """
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Mock failed login response
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.text = "Unauthorized: Invalid credentials"

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login (should raise)
            with pytest.raises(ValueError, match="Login failed: 401"):
                await session_manager.login(
                    connector=vcenter_connector, credentials=vcenter_credentials
                )


@pytest.mark.asyncio
class TestVCenterEndToEnd:
    """End-to-end tests simulating real vCenter usage"""

    async def test_vcenter_list_vms_flow(self, vcenter_connector, vcenter_credentials):
        """
        Simulate listing VMs from vCenter:
        1. Login with Basic Auth
        2. Get session token
        3. Call GET /api/vcenter/vm with session header
        """
        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Mock login response
            login_response = MagicMock()
            login_response.status_code = 200
            login_response.json.return_value = {"value": "session-token-123"}
            login_response.headers = {}
            login_response.cookies = {}

            # Mock VM list response
            vm_list_response = MagicMock()
            vm_list_response.status_code = 200
            vm_list_response.json.return_value = {
                "value": [
                    {"vm": "vm-1", "name": "test-vm-1", "power_state": "POWERED_ON"},
                    {"vm": "vm-2", "name": "test-vm-2", "power_state": "POWERED_OFF"},
                ]
            }
            vm_list_response.headers = {}

            # Setup mock client with multiple responses
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=[login_response, vm_list_response])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Step 1: Login
            session_token, _, _, _, _ = await session_manager.login(
                connector=vcenter_connector, credentials=vcenter_credentials
            )

            assert session_token == "session-token-123"

            # Step 2: Build auth headers
            auth_headers = session_manager.build_auth_headers(vcenter_connector, session_token)

            # Step 3: Make API call with session token
            async with mock_client:
                response = await mock_client.request(
                    method="GET",
                    url=f"{vcenter_connector.base_url}/api/vcenter/vm",
                    headers=auth_headers,
                )

            # Verify API call used session token
            api_call_kwargs = mock_client.request.call_args_list[1][1]  # Second call
            assert api_call_kwargs["headers"]["vmware-api-session-id"] == "session-token-123"

            # Verify response
            assert response.status_code == 200
            vm_data = response.json()
            assert len(vm_data["value"]) == 2
            assert vm_data["value"][0]["name"] == "test-vm-1"


@pytest.mark.asyncio
class TestBackwardCompatibilityIntegration:
    """Integration tests for backward compatibility with existing connectors"""

    async def test_standard_json_body_auth_still_works(self):
        """
        Verify existing SESSION connectors (JSON body auth) still work.
        """
        # Standard connector (no login_auth_type)
        standard_connector = Connector(
            id="conn-standard",
            tenant_id="test-tenant",
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
                "body_template": {"username": "{{username}}", "password": "{{password}}"},
                "token_location": "header",
                "token_name": "X-Auth-Token",
                "session_duration_seconds": 7200,
            },
            allowed_methods=["GET", "POST"],
            blocked_methods=[],
            default_safety_level="safe",
        )

        credentials = {"username": "user", "password": "pass"}

        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Mock response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"X-Auth-Token": "standard-token"}
            mock_response.cookies = {}
            mock_response.json.return_value = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Execute login
            session_token, _, _, _, state = await session_manager.login(
                connector=standard_connector, credentials=credentials
            )

            # Verify JSON body was used (not Basic Auth)
            call_kwargs = mock_client.request.call_args[1]
            assert "json" in call_kwargs
            assert call_kwargs["json"] == {"username": "user", "password": "pass"}

            # Verify success
            assert session_token == "standard-token"
            assert state == "LOGGED_IN"

    async def test_mixed_connector_types_coexist(self, vcenter_connector):
        """
        Test that vCenter (Basic Auth) and standard (body auth) connectors work side-by-side.
        """
        # This test verifies the architecture supports multiple auth patterns simultaneously

        # vCenter connector (Basic Auth)
        assert vcenter_connector.login_config["login_auth_type"] == "basic"

        # Standard connector (body auth)
        standard_connector = Connector(
            id="conn-standard-2",
            tenant_id="test-tenant",
            name="Another Standard API",
            base_url="https://api2.example.com",
            auth_type="SESSION",
            auth_config={},
            credential_strategy="USER_PROVIDED",
            is_active=True,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
            login_url="/login",
            login_method="POST",
            login_config={
                "body_template": {"user": "{{username}}", "pass": "{{password}}"},
                "token_location": "body",
                "token_path": "$.token",
                "session_duration_seconds": 1800,
            },
            allowed_methods=["GET"],
            blocked_methods=[],
            default_safety_level="safe",
        )

        session_manager = SessionManager()

        with patch("httpx.AsyncClient") as mock_client_class:
            # Mock responses for both connectors
            vcenter_response = MagicMock()
            vcenter_response.status_code = 200
            vcenter_response.json.return_value = {"value": "vcenter-token"}
            vcenter_response.headers = {}
            vcenter_response.cookies = {}

            standard_response = MagicMock()
            standard_response.status_code = 200
            standard_response.json.return_value = {"token": "standard-token"}
            standard_response.headers = {}
            standard_response.cookies = {}

            # Setup mock client
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=[vcenter_response, standard_response])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            # Login to vCenter (Basic Auth)
            vcenter_token, _, _, _, _ = await session_manager.login(
                connector=vcenter_connector,
                credentials={"username": "admin@vsphere.local", "password": "pass1"},
            )

            # Login to standard API (body auth)
            standard_token, _, _, _, _ = await session_manager.login(
                connector=standard_connector, credentials={"username": "user", "password": "pass2"}
            )

            # Verify both work correctly
            assert vcenter_token == "vcenter-token"
            assert standard_token == "standard-token"

            # Verify correct auth methods were used
            vcenter_call = mock_client.request.call_args_list[0][1]
            standard_call = mock_client.request.call_args_list[1][1]

            assert "auth" in vcenter_call  # Basic Auth
            assert "json" in standard_call  # Body auth
