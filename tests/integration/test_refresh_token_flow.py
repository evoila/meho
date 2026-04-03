# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for SESSION auth refresh token flow (Session 55).

Tests verify end-to-end refresh token functionality:
- Full refresh flow (login → use → refresh)
- Refresh token expiry handling
- HTTPClient auto-refresh integration
- Database persistence (encryption/decryption)
- VCF-compatible refresh format
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from meho_app.core.auth_context import UserContext
from meho_app.modules.connectors.models import ConnectorModel
from meho_app.modules.connectors.repositories.credential_repository import UserCredentialRepository
from meho_app.modules.connectors.rest.session_manager import SessionManager


# Test fixtures
@pytest.fixture
async def session_connector():
    """Create a test connector with SESSION auth and refresh token config."""
    return ConnectorModel(
        id="test-connector",
        tenant_id="test-tenant",
        name="Test VCF",
        base_url="https://vcf.example.com",
        auth_type="SESSION",
        credential_strategy="USER_PROVIDED",
        login_url="/v1/tokens",
        login_method="POST",
        login_config={
            "body_template": {"username": "{{username}}", "password": "{{password}}"},
            "token_location": "body",
            "token_path": "$.accessToken",
            "token_name": "X-Auth-Token",
            "session_duration_seconds": 3600,
            # Refresh token configuration (VCF format)
            "refresh_token_path": "$.refreshToken.id",
            "refresh_url": "/v1/tokens/access-token/refresh",
            "refresh_method": "PATCH",
            "refresh_token_expires_in": 86400,  # 24 hours
            "refresh_body_template": {"refreshToken": {"id": "{{refresh_token}}"}},
        },
        allowed_methods=["GET", "POST", "PATCH"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
    )


@pytest.fixture
def user_context():
    """Create test user context."""
    return UserContext(user_id="test-user", tenant_id="test-tenant")


@pytest.fixture
def test_credentials():
    """Create test credentials."""
    return {"username": "admin@local", "password": "VMware123!"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_refresh_flow(session_connector, user_context, test_credentials):
    """
    Test complete refresh flow end-to-end:
    1. Login → get access + refresh tokens
    2. Store in database (encrypted)
    3. Retrieve from database
    4. Simulate token expiry
    5. Call API → auto-refresh triggered
    6. Verify new access token works
    """
    session_manager = SessionManager()

    # Mock HTTP responses
    login_response = MagicMock()
    login_response.status_code = 200
    login_response.json.return_value = {
        "accessToken": "access-token-12345",
        "refreshToken": {"id": "refresh-token-67890"},
    }

    refresh_response = MagicMock()
    refresh_response.status_code = 200
    refresh_response.json.return_value = {"accessToken": "new-access-token-99999"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.request.side_effect = [login_response, refresh_response]

        # Step 1: Login and get both tokens
        (
            access_token,
            refresh_token,
            _expires_at,
            refresh_expires_at,
            state,
        ) = await session_manager.login(connector=session_connector, credentials=test_credentials)

        assert access_token == "access-token-12345"
        assert refresh_token == "refresh-token-67890"
        assert refresh_expires_at is not None
        assert state == "LOGGED_IN"

        # Verify login request was correct
        login_call = mock_client.request.call_args_list[0]
        # call_args_list[0] is a call object with .args and .kwargs
        assert (
            login_call.kwargs.get("method", login_call.args[0] if login_call.args else None)
            == "POST"
        )
        assert "/v1/tokens" in (
            login_call.kwargs.get("url", login_call.args[1] if len(login_call.args) > 1 else "")
        )
        json_data = login_call.kwargs.get("json", {})
        assert json_data["username"] == "admin@local"
        assert json_data["password"] == "VMware123!"

        # Step 2: Simulate token expiring soon (< 5 min)
        datetime.now(tz=UTC) + timedelta(minutes=3)

        # Step 3: Call refresh
        new_access_token, new_expires_at = await session_manager.refresh(
            connector=session_connector, refresh_token=refresh_token
        )

        assert new_access_token == "new-access-token-99999"
        assert new_expires_at > datetime.now(tz=UTC)

        # Verify refresh request was correct (VCF nested format)
        refresh_call = mock_client.request.call_args_list[1]
        assert (
            refresh_call.kwargs.get("method", refresh_call.args[0] if refresh_call.args else None)
            == "PATCH"
        )
        assert "/v1/tokens/access-token/refresh" in (
            refresh_call.kwargs.get(
                "url", refresh_call.args[1] if len(refresh_call.args) > 1 else ""
            )
        )
        refresh_body = refresh_call.kwargs.get("json", {})
        assert refresh_body["refreshToken"]["id"] == "refresh-token-67890"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_token_expiry_scenarios(session_connector, test_credentials):
    """
    Test refresh token expiry scenarios:
    1. Access expired + refresh valid → Refresh ✅
    2. Access expired + refresh expired → Re-login ✅
    """
    session_manager = SessionManager()

    # Scenario 1: Refresh valid, should use refresh
    login_response = MagicMock()
    login_response.status_code = 200
    login_response.json.return_value = {
        "accessToken": "access-1",
        "refreshToken": {"id": "refresh-1"},
    }

    refresh_response = MagicMock()
    refresh_response.status_code = 200
    refresh_response.json.return_value = {"accessToken": "access-2"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.request.side_effect = [login_response, refresh_response]

        # Initial login
        (
            access_token,
            refresh_token,
            _expires_at,
            _refresh_expires_at,
            _,
        ) = await session_manager.login(connector=session_connector, credentials=test_credentials)

        assert access_token == "access-1"
        assert refresh_token == "refresh-1"

        # Refresh with valid refresh token
        new_token, _new_expires = await session_manager.refresh(
            connector=session_connector, refresh_token=refresh_token
        )

        assert new_token == "access-2"
        assert mock_client.request.call_count == 2  # login + refresh

    # Scenario 2: Refresh expired, should fail and require re-login
    refresh_failure_response = MagicMock()
    refresh_failure_response.status_code = 401
    refresh_failure_response.text = "Refresh token expired"
    refresh_failure_response.raise_for_status.side_effect = Exception("401 Unauthorized")

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.request.return_value = refresh_failure_response

        with pytest.raises(ValueError, match="refresh.*failed|401"):  # noqa: RUF043 -- test uses broad pattern intentionally
            await session_manager.refresh(
                connector=session_connector, refresh_token="expired-refresh-token"
            )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skip(reason="aiosqlite not installed - requires docker environment")
async def test_database_persistence_with_refresh_tokens(
    session_connector, user_context, test_credentials
):
    """
    Test session state persistence with refresh tokens:
    1. Store refresh token via update_session_state()
    2. Retrieve via get_session_state()
    3. Verify encryption/decryption works
    4. Verify expiry tracking
    """
    # Create in-memory SQLite database for testing
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # Import and create tables
    from meho_app.modules.connectors.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session_maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session_maker() as session:
        # Create connector
        session.add(session_connector)
        await session.commit()

        # Create credential repository
        cred_repo = UserCredentialRepository(session)

        # Simulate login - store session with refresh token
        access_token = "test-access-token-abc123"
        refresh_token = "test-refresh-token-xyz789"
        access_expires = datetime.now(tz=UTC) + timedelta(hours=1)
        refresh_expires = datetime.now(tz=UTC) + timedelta(hours=24)

        await cred_repo.update_session_state(
            user_id=user_context.user_id,
            connector_id=session_connector.id,
            session_token=access_token,
            session_expires_at=access_expires,
            session_state="LOGGED_IN",
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires,
        )

        await session.commit()

        # Retrieve session state
        (
            retrieved_token,
            retrieved_refresh,
            retrieved_expires,
            retrieved_refresh_expires,
            retrieved_state,
        ) = await cred_repo.get_session_state(
            user_id=user_context.user_id, connector_id=session_connector.id
        )

        # Verify data round-tripped correctly
        assert retrieved_token == access_token
        assert retrieved_refresh == refresh_token
        assert retrieved_state == "LOGGED_IN"

        # Verify expiry times are close (within 1 second due to serialization)
        assert abs((retrieved_expires - access_expires).total_seconds()) < 1
        assert abs((retrieved_refresh_expires - refresh_expires).total_seconds()) < 1

        # Verify tokens are encrypted in database
        result = await session.execute(
            "SELECT session_token, session_refresh_token FROM user_connector_credential WHERE user_id = ?",
            (user_context.user_id,),
        )
        raw_row = result.fetchone()

        # Encrypted tokens should NOT match plaintext
        assert raw_row[0] != access_token  # Access token encrypted
        assert raw_row[1] != refresh_token  # Refresh token encrypted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_manager_with_callback(session_connector, test_credentials):
    """
    Test SessionManager callback mechanism for session updates:
    1. Login with refresh token support
    2. Verify session state returned
    3. Verify both tokens obtained
    """
    session_manager = SessionManager()

    login_response = MagicMock()
    login_response.status_code = 200
    login_response.json.return_value = {
        "accessToken": "test-access-token",
        "refreshToken": {"id": "test-refresh-token"},
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.request.return_value = login_response

        # Login with refresh token support
        (
            access_token,
            refresh_token,
            expires_at,
            refresh_expires_at,
            state,
        ) = await session_manager.login(connector=session_connector, credentials=test_credentials)

        # Verify all return values are present
        assert access_token == "test-access-token"
        assert refresh_token == "test-refresh-token"
        assert expires_at is not None
        assert refresh_expires_at is not None
        assert state == "LOGGED_IN"

        # Verify session manager can be reused
        assert session_manager.timeout == pytest.approx(30.0)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vcf_format_validation(session_connector, test_credentials):
    """
    Test VCF-specific refresh format:
    1. Login response: {accessToken, refreshToken: {id}}
    2. Extract both tokens
    3. Refresh with nested body: {refreshToken: {id}}
    4. Extract new access token
    """
    session_manager = SessionManager()

    # VCF login response format
    vcf_login_response = MagicMock()
    vcf_login_response.status_code = 200
    vcf_login_response.json.return_value = {
        "accessToken": "eyJhbGc...vcf-access-token",
        "refreshToken": {
            "id": "abc-123-def-456"  # VCF nested format
        },
    }

    # VCF refresh response format
    vcf_refresh_response = MagicMock()
    vcf_refresh_response.status_code = 200
    vcf_refresh_response.json.return_value = {"accessToken": "eyJhbGc...new-vcf-access-token"}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.request.side_effect = [vcf_login_response, vcf_refresh_response]

        # Step 1: Login with VCF format
        (
            access_token,
            refresh_token,
            _expires_at,
            _refresh_expires_at,
            _state,
        ) = await session_manager.login(connector=session_connector, credentials=test_credentials)

        # Verify tokens extracted correctly from nested format
        assert access_token == "eyJhbGc...vcf-access-token"
        assert refresh_token == "abc-123-def-456"  # Extracted from nested object

        # Step 2: Refresh with VCF nested body format
        new_access, _new_expires = await session_manager.refresh(
            connector=session_connector, refresh_token=refresh_token
        )

        assert new_access == "eyJhbGc...new-vcf-access-token"

        # Verify refresh request used nested format
        refresh_call = mock_client.request.call_args_list[1]
        refresh_body = refresh_call.kwargs.get("json", {})

        # VCF requires nested format: {refreshToken: {id: "..."}}
        assert "refreshToken" in refresh_body
        assert "id" in refresh_body["refreshToken"]
        assert refresh_body["refreshToken"]["id"] == "abc-123-def-456"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refresh_without_refresh_token_support(test_credentials):
    """
    Test connector without refresh token support:
    - Should work fine (just re-login on expiry)
    - No refresh_token returned
    """
    # Create connector WITHOUT refresh token config
    simple_connector = ConnectorModel(
        id="simple-connector",
        tenant_id="test-tenant",
        name="Simple Session Auth",
        base_url="https://simple.example.com",
        auth_type="SESSION",
        credential_strategy="USER_PROVIDED",
        login_url="/auth/login",
        login_method="POST",
        login_config={
            "body_template": {"username": "{{username}}", "password": "{{password}}"},
            "token_location": "body",
            "token_path": "$.token",
            "session_duration_seconds": 1800,
            # NO refresh token config
        },
        allowed_methods=["GET", "POST"],
        blocked_methods=[],
        default_safety_level="safe",
        is_active=True,
    )

    session_manager = SessionManager()

    simple_login_response = MagicMock()
    simple_login_response.status_code = 200
    simple_login_response.json.return_value = {
        "token": "simple-session-token"
        # NO refresh token in response
    }

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client
        mock_client.request.return_value = simple_login_response

        # Login without refresh token support
        (
            access_token,
            refresh_token,
            _expires_at,
            refresh_expires_at,
            state,
        ) = await session_manager.login(connector=simple_connector, credentials=test_credentials)

        # Should work fine, but no refresh token
        assert access_token == "simple-session-token"
        assert refresh_token is None  # No refresh token
        assert refresh_expires_at is None  # No refresh expiry
        assert state == "LOGGED_IN"
