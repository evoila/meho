# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for SessionManager.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from meho_app.modules.connectors.rest.session_manager import SessionManager
from meho_app.modules.connectors.schemas import Connector


@pytest.fixture
def session_connector():
    """Fixture for SESSION auth connector"""
    return Connector(
        id="test-connector-id",
        tenant_id="test-tenant",
        name="Test SESSION Connector",
        description="Test connector with SESSION auth",
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
            "header_name": "X-Auth-Token",  # Specify header name for requests
            "session_duration_seconds": 3600,
        },
        allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def user_credentials():
    """Fixture for user credentials"""
    return {"username": "test_user", "password": "test_password"}


@pytest.mark.asyncio
async def test_session_manager_login_success(session_connector, user_credentials):
    """Test successful login with SESSION auth"""
    session_manager = SessionManager()

    # Mock httpx response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "test-session-token-12345"}
    mock_response.headers = {"X-Auth-Token": "test-session-token-12345"}
    mock_response.cookies = {}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Perform login
        token, refresh_token, expires_at, refresh_expires_at, state = await session_manager.login(
            connector=session_connector, credentials=user_credentials
        )

        # Verify results
        assert token == "test-session-token-12345"
        assert expires_at > datetime.now(tz=UTC)
        assert expires_at < datetime.now(tz=UTC) + timedelta(seconds=3700)
        assert state == "LOGGED_IN"
        # No refresh token in this test (not configured)
        assert refresh_token is None
        assert refresh_expires_at is None

        # Verify request was made correctly
        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[1]["method"] == "POST"
        assert "/api/v1/auth/login" in call_args[1]["url"]
        assert call_args[1]["json"] == {"username": "test_user", "password": "test_password"}


@pytest.mark.asyncio
async def test_session_manager_login_failure(session_connector, user_credentials):
    """Test login failure with SESSION auth"""
    session_manager = SessionManager()

    # Mock httpx response with 401
    mock_response = Mock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized: Invalid credentials"

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Perform login - should raise ValueError
        with pytest.raises(ValueError, match="Login failed"):
            await session_manager.login(connector=session_connector, credentials=user_credentials)


@pytest.mark.asyncio
async def test_session_manager_reuse_valid_token(session_connector, user_credentials):
    """Test that valid session token is reused"""
    session_manager = SessionManager()

    # Token that expires in 10 minutes (more than 5 min threshold)
    valid_expires_at = datetime.now(tz=UTC) + timedelta(minutes=10)
    existing_token = "existing-valid-token"

    # Should return existing token without making HTTP request
    token, refresh_token, expires_at, refresh_expires_at, state = await session_manager.login(
        connector=session_connector,
        credentials=user_credentials,
        session_token=existing_token,
        session_expires_at=valid_expires_at,
    )

    assert token == existing_token
    assert expires_at == valid_expires_at
    assert state == "LOGGED_IN"
    # Should return None for refresh tokens (not passed in)
    assert refresh_token is None
    assert refresh_expires_at is None


@pytest.mark.asyncio
async def test_session_manager_refresh_expired_token(session_connector, user_credentials):
    """Test that expired session token triggers re-login"""
    session_manager = SessionManager()

    # Token that expires in 2 minutes (less than 5 min threshold)
    expired_expires_at = datetime.now(tz=UTC) + timedelta(minutes=2)
    existing_token = "expiring-token"

    # Mock httpx response for re-login
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "new-session-token"}
    mock_response.headers = {"X-Auth-Token": "new-session-token"}
    mock_response.cookies = {}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Should trigger re-login
        token, refresh_token, expires_at, refresh_expires_at, state = await session_manager.login(
            connector=session_connector,
            credentials=user_credentials,
            session_token=existing_token,
            session_expires_at=expired_expires_at,
        )

        assert token == "new-session-token"
        assert token != existing_token
        assert expires_at > datetime.now(tz=UTC) + timedelta(minutes=55)
        assert state == "LOGGED_IN"
        # No refresh token configured in this test
        assert refresh_token is None
        assert refresh_expires_at is None


def test_build_login_body(session_connector, user_credentials):
    """Test login body construction from template"""
    session_manager = SessionManager()

    body = session_manager._build_login_body(session_connector.login_config, user_credentials)

    assert body == {"username": "test_user", "password": "test_password"}


def test_extract_session_token_from_header():
    """Test extracting session token from response header"""
    session_manager = SessionManager()

    login_config = {"token_location": "header", "token_name": "X-Auth-Token"}

    headers = {"X-Auth-Token": "test-token-123"}

    token = session_manager._extract_session_token(
        login_config, response_data={}, headers=headers, cookies={}
    )

    assert token == "test-token-123"


def test_extract_session_token_from_body():
    """Test extracting session token from response body"""
    session_manager = SessionManager()

    login_config = {"token_location": "body", "token_name": "token", "token_path": "$.token"}

    response_data = {"token": "test-token-456"}

    token = session_manager._extract_session_token(
        login_config, response_data=response_data, headers={}, cookies={}
    )

    assert token == "test-token-456"


def test_extract_session_token_from_nested_body():
    """Test extracting session token from nested response body"""
    session_manager = SessionManager()

    login_config = {
        "token_location": "body",
        "token_name": "auth_token",
        "token_path": "$.data.auth_token",
    }

    response_data = {"data": {"auth_token": "nested-token-789"}}

    token = session_manager._extract_session_token(
        login_config, response_data=response_data, headers={}, cookies={}
    )

    assert token == "nested-token-789"


def test_build_auth_headers(session_connector):
    """Test building auth headers with session token"""
    session_manager = SessionManager()

    headers = session_manager.build_auth_headers(session_connector, "my-session-token")

    assert headers == {"X-Auth-Token": "my-session-token"}


# ==================== REFRESH TOKEN TESTS ====================


def test_extract_refresh_token_flat():
    """Test extracting refresh token from flat response body"""
    session_manager = SessionManager()

    login_config = {"refresh_token_path": "$.refresh_token"}

    response_data = {"refresh_token": "refresh-token-12345"}

    token = session_manager._extract_refresh_token(login_config, response_data)

    assert token == "refresh-token-12345"


def test_extract_refresh_token_nested():
    """Test extracting refresh token from nested response body (VCF format)"""
    session_manager = SessionManager()

    login_config = {"refresh_token_path": "$.refreshToken.id"}

    response_data = {
        "accessToken": "access-token-12345",
        "refreshToken": {"id": "9842392f-c568-42df-a236-491fa00a6d4"},
    }

    token = session_manager._extract_refresh_token(login_config, response_data)

    assert token == "9842392f-c568-42df-a236-491fa00a6d4"


def test_extract_refresh_token_missing():
    """Test extracting refresh token when not present in response"""
    session_manager = SessionManager()

    login_config = {"refresh_token_path": "$.refresh_token"}

    response_data = {"access_token": "only-access-token"}

    token = session_manager._extract_refresh_token(login_config, response_data)

    assert token is None


def test_extract_refresh_token_no_config():
    """Test extracting refresh token when not configured"""
    session_manager = SessionManager()

    login_config = {}  # No refresh_token_path
    response_data = {"refresh_token": "refresh-token-12345"}

    token = session_manager._extract_refresh_token(login_config, response_data)

    assert token is None


@pytest.mark.asyncio
async def test_login_with_refresh_token():
    """Test login that returns both access and refresh tokens"""
    connector = Connector(
        id="test-connector-id",
        tenant_id="test-tenant",
        name="Test Connector with Refresh",
        description="Test connector",
        base_url="https://api.example.com",
        auth_type="SESSION",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        login_url="/api/v1/auth/login",
        login_method="POST",
        login_config={
            "body_template": {"username": "{{username}}", "password": "{{password}}"},
            "token_location": "body",
            "token_path": "$.accessToken",
            "refresh_token_path": "$.refreshToken.id",
            "session_duration_seconds": 3600,
            "refresh_token_expires_in": 86400,
        },
        allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    session_manager = SessionManager()

    # Mock httpx response with both tokens (VCF format)
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "accessToken": "eyJhbGc...",
        "refreshToken": {"id": "9842392f-c568-42df-a236-491fa00a6d4"},
    }
    mock_response.headers = {}
    mock_response.cookies = {}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Perform login
        token, refresh_token, expires_at, refresh_expires_at, state = await session_manager.login(
            connector=connector, credentials={"username": "admin", "password": "pass"}
        )

        # Verify both tokens extracted
        assert token == "eyJhbGc..."
        assert refresh_token == "9842392f-c568-42df-a236-491fa00a6d4"
        assert expires_at > datetime.now(tz=UTC)
        assert expires_at < datetime.now(tz=UTC) + timedelta(seconds=3700)
        assert refresh_expires_at > datetime.now(tz=UTC) + timedelta(hours=23)
        assert refresh_expires_at < datetime.now(tz=UTC) + timedelta(hours=25)
        assert state == "LOGGED_IN"


@pytest.mark.asyncio
async def test_refresh_success():
    """Test successful token refresh"""
    connector = Connector(
        id="test-connector-id",
        tenant_id="test-tenant",
        name="Test Connector",
        description="Test connector",
        base_url="https://api.example.com",
        auth_type="SESSION",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        login_url="/api/v1/auth/login",
        login_method="POST",
        login_config={
            "token_location": "body",
            "token_path": "$.accessToken",
            "refresh_url": "/api/v1/tokens/refresh",
            "refresh_method": "PATCH",
            "refresh_body_template": {"refreshToken": {"id": "{{refresh_token}}"}},
            "session_duration_seconds": 3600,
        },
        allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    session_manager = SessionManager()

    # Mock httpx response with new access token
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"accessToken": "new-access-token-xyz"}
    mock_response.headers = {}
    mock_response.cookies = {}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Perform refresh
        new_token, new_expires = await session_manager.refresh(
            connector=connector, refresh_token="old-refresh-token-abc"
        )

        # Verify new token
        assert new_token == "new-access-token-xyz"
        assert new_expires > datetime.now(tz=UTC)
        assert new_expires < datetime.now(tz=UTC) + timedelta(seconds=3700)

        # Verify request was made correctly
        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[1]["method"] == "PATCH"
        assert "/api/v1/tokens/refresh" in call_args[1]["url"]
        assert call_args[1]["json"] == {"refreshToken": {"id": "old-refresh-token-abc"}}


@pytest.mark.asyncio
async def test_refresh_failure():
    """Test refresh failure (e.g., refresh token expired)"""
    connector = Connector(
        id="test-connector-id",
        tenant_id="test-tenant",
        name="Test Connector",
        description="Test connector",
        base_url="https://api.example.com",
        auth_type="SESSION",
        auth_config={},
        credential_strategy="USER_PROVIDED",
        login_url="/api/v1/auth/login",
        login_method="POST",
        login_config={
            "token_location": "body",
            "token_path": "$.accessToken",
            "refresh_url": "/api/v1/tokens/refresh",
            "refresh_method": "POST",
            "session_duration_seconds": 3600,
        },
        allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    session_manager = SessionManager()

    # Mock httpx response with 401
    mock_response = Mock()
    mock_response.status_code = 401
    mock_response.text = "Refresh token expired"

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Perform refresh - should raise ValueError
        with pytest.raises(ValueError, match="Token refresh failed"):
            await session_manager.refresh(
                connector=connector, refresh_token="expired-refresh-token"
            )


def test_build_refresh_body_simple():
    """Test building refresh request body with simple template"""
    session_manager = SessionManager()

    login_config = {"refresh_body_template": {"refresh_token": "{{refresh_token}}"}}

    body = session_manager._build_refresh_body(login_config, "my-refresh-token")

    assert body == {"refresh_token": "my-refresh-token"}


def test_build_refresh_body_nested():
    """Test building refresh request body with nested template (VCF format)"""
    session_manager = SessionManager()

    login_config = {"refresh_body_template": {"refreshToken": {"id": "{{refresh_token}}"}}}

    body = session_manager._build_refresh_body(login_config, "9842392f-c568-42df")

    assert body == {"refreshToken": {"id": "9842392f-c568-42df"}}


def test_build_refresh_body_default():
    """Test building refresh request body with no template (default)"""
    session_manager = SessionManager()

    login_config = {}  # No refresh_body_template

    body = session_manager._build_refresh_body(login_config, "default-token")

    assert body == {"refresh_token": "default-token"}


def test_replace_template_vars_dict():
    """Test recursive template variable replacement in dict"""
    session_manager = SessionManager()

    template = {"token": "{{refresh_token}}", "nested": {"value": "{{refresh_token}}"}}

    result = session_manager._replace_template_vars(template, {"refresh_token": "abc123"})

    assert result == {"token": "abc123", "nested": {"value": "abc123"}}


def test_replace_template_vars_list():
    """Test recursive template variable replacement in list"""
    session_manager = SessionManager()

    template = ["{{refresh_token}}", "static", {"key": "{{refresh_token}}"}]

    result = session_manager._replace_template_vars(template, {"refresh_token": "xyz789"})

    assert result == ["xyz789", "static", {"key": "xyz789"}]


def test_replace_template_vars_string():
    """Test template variable replacement in string"""
    session_manager = SessionManager()

    template = "{{refresh_token}}"

    result = session_manager._replace_template_vars(template, {"refresh_token": "token123"})

    assert result == "token123"


def test_replace_template_vars_non_template():
    """Test that non-template strings are not replaced"""
    session_manager = SessionManager()

    template = "this is not a template"

    result = session_manager._replace_template_vars(template, {"refresh_token": "token123"})

    assert result == "this is not a template"
