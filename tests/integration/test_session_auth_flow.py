# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Integration tests for SESSION-based authentication flow.

Tests the complete flow:
1. SessionManager login
2. Token storage in user_connector_credential
3. HTTP client auto-login
4. Token reuse
5. Token expiry and refresh
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from meho_app.modules.connectors.repositories.credential_repository import UserCredentialRepository
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
from meho_app.modules.connectors.rest.schemas import EndpointDescriptor
from meho_app.modules.connectors.rest.session_manager import SessionManager
from meho_app.modules.connectors.schemas import Connector, UserCredentialProvide


@pytest.fixture
def session_connector():
    """Fixture for SESSION auth connector"""
    return Connector(
        id="12345678-1234-1234-1234-123456789abc",
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
def test_endpoint():
    """Fixture for test endpoint"""
    return EndpointDescriptor(
        id="87654321-4321-4321-4321-cba987654321",
        connector_id="12345678-1234-1234-1234-123456789abc",
        method="GET",
        path="/api/v1/clusters",
        operation_id="getClusters",
        summary="Get all clusters",
        description="Returns list of all clusters",
        tags=["clusters"],
        required_params=[],
        path_params_schema={},
        query_params_schema={},
        body_schema={},
        response_schema={},
        is_enabled=True,
        safety_level="safe",
        requires_approval=False,
        created_at=datetime.now(tz=UTC),
    )


@pytest.fixture
def user_credentials():
    """Fixture for user credentials"""
    return {"username": "test_user", "password": "test_password"}


@pytest.mark.asyncio
async def test_session_auth_end_to_end(
    db_session, session_connector, test_endpoint, user_credentials
):
    """
    Test complete SESSION auth flow.

    Steps:
    1. Create connector with SESSION auth
    2. Store user credentials
    3. Call HTTP client
    4. Verify auto-login happens
    5. Verify session token stored
    6. Make second call
    7. Verify token reused (no second login)
    """
    user_id = "test-user-123"

    # Setup: Create connector in database first (for foreign key)
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.schemas import ConnectorCreate

    connector_repo = ConnectorRepository(db_session)
    created_connector = await connector_repo.create_connector(
        ConnectorCreate(
            tenant_id=session_connector.tenant_id,
            name=session_connector.name,
            base_url=session_connector.base_url,
            auth_type=session_connector.auth_type,
            description=session_connector.description,
            auth_config=session_connector.auth_config,
            credential_strategy=session_connector.credential_strategy,
            login_url=session_connector.login_url,
            login_method=session_connector.login_method,
            login_config=session_connector.login_config,
            allowed_methods=session_connector.allowed_methods,
            blocked_methods=session_connector.blocked_methods,
            default_safety_level=session_connector.default_safety_level,
        )
    )

    # Use the created connector's ID
    connector_id = created_connector.id
    session_connector.id = connector_id

    # Setup: Store credentials
    cred_repo = UserCredentialRepository(db_session)
    await cred_repo.store_credentials(
        user_id=user_id,
        credential=UserCredentialProvide(
            connector_id=connector_id, credential_type="SESSION", credentials=user_credentials
        ),
    )

    # Mock HTTP responses
    mock_login_response = Mock()
    mock_login_response.status_code = 200
    mock_login_response.json.return_value = {"token": "session-token-12345"}
    mock_login_response.headers = {"X-Auth-Token": "session-token-12345"}
    mock_login_response.cookies = {}

    mock_api_response = Mock()
    mock_api_response.status_code = 200
    mock_api_response.json.return_value = {"clusters": [{"id": "1", "name": "cluster-1"}]}
    mock_api_response.headers = {}  # Add headers dict
    mock_api_response.text = '{"clusters": [{"id": "1", "name": "cluster-1"}]}'

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        # First call: login, second call: API request, third call: API request (reuse token)
        mock_client.request.side_effect = [
            mock_login_response,  # Login call
            mock_api_response,  # First API call
            mock_api_response,  # Second API call (should reuse token)
        ]
        mock_client_class.return_value = mock_client

        # Callback to capture session updates
        session_updates = []

        def on_session_update(token, expires_at, state):
            session_updates.append({"token": token, "expires_at": expires_at, "state": state})

        # First API call - should trigger login
        http_client = GenericHTTPClient()
        status_code, data = await http_client.call_endpoint(
            connector=session_connector,
            endpoint=test_endpoint,
            user_credentials=user_credentials,
            session_token=None,
            session_expires_at=None,
            on_session_update=on_session_update,
        )

        # Verify login was called
        assert mock_client.request.call_count == 2  # Login + API call
        login_call = mock_client.request.call_args_list[0]
        assert "/api/v1/auth/login" in login_call[1]["url"]
        assert login_call[1]["json"] == {"username": "test_user", "password": "test_password"}

        # Verify session update callback was called
        assert len(session_updates) == 1
        assert session_updates[0]["token"] == "session-token-12345"
        assert session_updates[0]["state"] == "LOGGED_IN"

        # Verify API call succeeded
        assert status_code == 200
        assert data["clusters"][0]["name"] == "cluster-1"

        # Second API call with valid token - should NOT re-login
        mock_client.request.reset_mock()
        mock_client.request.return_value = mock_api_response

        _status_code2, _data2 = await http_client.call_endpoint(
            connector=session_connector,
            endpoint=test_endpoint,
            user_credentials=user_credentials,
            session_token="session-token-12345",
            session_expires_at=datetime.now(tz=UTC) + timedelta(hours=1),  # Still valid
            on_session_update=on_session_update,
        )

        # Verify NO login call was made (token reused)
        assert mock_client.request.call_count == 1  # Only API call, no login
        api_call = mock_client.request.call_args
        assert "/api/v1/clusters" in api_call[1]["url"]
        assert "X-Auth-Token" in api_call[1]["headers"]
        assert api_call[1]["headers"]["X-Auth-Token"] == "session-token-12345"


@pytest.mark.asyncio
async def test_session_token_expiry_triggers_relogin(
    db_session, session_connector, test_endpoint, user_credentials
):
    """
    Test that expired tokens trigger automatic re-login.

    Steps:
    1. Login and get token
    2. Set token expiry to near-expiry (< 5 min)
    3. Make API call
    4. Verify re-login happens automatically
    5. Verify new token is used
    """
    # Mock HTTP responses
    mock_login_response = Mock()
    mock_login_response.status_code = 200
    mock_login_response.json.return_value = {"token": "new-session-token-67890"}
    mock_login_response.headers = {"X-Auth-Token": "new-session-token-67890"}
    mock_login_response.cookies = {}

    mock_api_response = Mock()
    mock_api_response.status_code = 200
    mock_api_response.json.return_value = {"clusters": []}
    mock_api_response.headers = {}  # Add headers dict
    mock_api_response.text = '{"clusters": []}'

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.side_effect = [mock_login_response, mock_api_response]
        mock_client_class.return_value = mock_client

        session_updates = []

        def on_session_update(token, expires_at, state):
            session_updates.append({"token": token})

        # Make API call with expiring token (< 5 minutes)
        http_client = GenericHTTPClient()
        expired_token = "old-expired-token"
        expiry = datetime.now(tz=UTC) + timedelta(minutes=2)  # < 5 min threshold

        _status_code, _data = await http_client.call_endpoint(
            connector=session_connector,
            endpoint=test_endpoint,
            user_credentials=user_credentials,
            session_token=expired_token,
            session_expires_at=expiry,
            on_session_update=on_session_update,
        )

        # Verify re-login happened
        assert mock_client.request.call_count == 2  # Re-login + API call

        # Verify session was updated with new token
        assert len(session_updates) == 1
        assert session_updates[0]["token"] == "new-session-token-67890"
        assert session_updates[0]["token"] != expired_token


@pytest.mark.asyncio
async def test_user_session_isolation(db_session, session_connector):
    """
    Test that users cannot access each other's session tokens.

    Steps:
    1. Alice logs in → gets token A
    2. Bob logs in → gets token B
    3. Verify token A ≠ token B
    4. Verify Alice cannot access Bob's credentials
    5. Verify Bob cannot access Alice's credentials
    """
    alice_id = "alice-user-123"
    bob_id = "bob-user-456"

    alice_creds = {"username": "alice", "password": "alice_pass"}
    bob_creds = {"username": "bob", "password": "bob_pass"}

    # Setup: Create connector in database first
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.schemas import ConnectorCreate

    connector_repo = ConnectorRepository(db_session)
    created_connector = await connector_repo.create_connector(
        ConnectorCreate(
            tenant_id=session_connector.tenant_id,
            name=session_connector.name,
            base_url=session_connector.base_url,
            auth_type=session_connector.auth_type,
            auth_config=session_connector.auth_config,
            credential_strategy=session_connector.credential_strategy,
            login_url=session_connector.login_url,
            login_method=session_connector.login_method,
            login_config=session_connector.login_config,
            allowed_methods=session_connector.allowed_methods,
            blocked_methods=session_connector.blocked_methods,
            default_safety_level=session_connector.default_safety_level,
        )
    )

    connector_id = created_connector.id

    cred_repo = UserCredentialRepository(db_session)

    # Alice stores her credentials
    await cred_repo.store_credentials(
        user_id=alice_id,
        credential=UserCredentialProvide(
            connector_id=connector_id, credential_type="SESSION", credentials=alice_creds
        ),
    )

    # Bob stores his credentials
    await cred_repo.store_credentials(
        user_id=bob_id,
        credential=UserCredentialProvide(
            connector_id=connector_id, credential_type="SESSION", credentials=bob_creds
        ),
    )

    # Simulate logins and token storage
    alice_token = "alice-session-token-aaa"
    alice_expiry = datetime.now(tz=UTC) + timedelta(hours=1)

    bob_token = "bob-session-token-bbb"
    bob_expiry = datetime.now(tz=UTC) + timedelta(hours=1)

    await cred_repo.update_session_state(
        user_id=alice_id,
        connector_id=connector_id,
        session_token=alice_token,
        session_expires_at=alice_expiry,
        session_state="LOGGED_IN",
    )

    await cred_repo.update_session_state(
        user_id=bob_id,
        connector_id=connector_id,
        session_token=bob_token,
        session_expires_at=bob_expiry,
        session_state="LOGGED_IN",
    )

    # Verify Alice's session
    alice_session = await cred_repo.get_session_state(alice_id, connector_id)
    assert alice_session is not None
    assert alice_session["session_token"] == alice_token
    assert alice_session["session_state"] == "LOGGED_IN"

    # Verify Bob's session
    bob_session = await cred_repo.get_session_state(bob_id, connector_id)
    assert bob_session is not None
    assert bob_session["session_token"] == bob_token
    assert bob_session["session_state"] == "LOGGED_IN"

    # Verify tokens are different
    assert alice_session["session_token"] != bob_session["session_token"]

    # Verify Alice cannot access Bob's credentials
    alice_retrieved = await cred_repo.get_credentials(alice_id, connector_id)
    assert alice_retrieved["username"] == "alice"
    assert alice_retrieved["username"] != "bob"

    # Verify Bob cannot access Alice's credentials
    bob_retrieved = await cred_repo.get_credentials(bob_id, connector_id)
    assert bob_retrieved["username"] == "bob"
    assert bob_retrieved["username"] != "alice"


@pytest.mark.asyncio
async def test_session_state_persistence(db_session, session_connector, user_credentials):
    """
    Test that session state is properly stored and retrieved.

    Steps:
    1. Store credentials
    2. Update session state via SessionManager
    3. Retrieve session state
    4. Verify session_token is present
    5. Verify session_expires_at is set
    6. Verify session_state is "LOGGED_IN"
    """
    user_id = "test-user-persistence"

    # Setup: Create connector in database first
    from meho_app.modules.connectors.repositories import ConnectorRepository
    from meho_app.modules.connectors.schemas import ConnectorCreate

    connector_repo = ConnectorRepository(db_session)
    created_connector = await connector_repo.create_connector(
        ConnectorCreate(
            tenant_id=session_connector.tenant_id,
            name=session_connector.name,
            base_url=session_connector.base_url,
            auth_type=session_connector.auth_type,
            auth_config=session_connector.auth_config,
            credential_strategy=session_connector.credential_strategy,
            login_url=session_connector.login_url,
            login_method=session_connector.login_method,
            login_config=session_connector.login_config,
            allowed_methods=session_connector.allowed_methods,
            blocked_methods=session_connector.blocked_methods,
            default_safety_level=session_connector.default_safety_level,
        )
    )

    connector_id = created_connector.id

    cred_repo = UserCredentialRepository(db_session)

    # Store initial credentials
    await cred_repo.store_credentials(
        user_id=user_id,
        credential=UserCredentialProvide(
            connector_id=connector_id, credential_type="SESSION", credentials=user_credentials
        ),
    )

    # Update session state (simulating successful login)
    session_token = "persistent-token-xyz"
    session_expiry = datetime.now(tz=UTC) + timedelta(hours=1)

    await cred_repo.update_session_state(
        user_id=user_id,
        connector_id=connector_id,
        session_token=session_token,
        session_expires_at=session_expiry,
        session_state="LOGGED_IN",
    )

    # Retrieve session state
    session_state = await cred_repo.get_session_state(user_id, connector_id)

    # Verify session state
    assert session_state is not None
    assert session_state["session_token"] == session_token
    assert session_state["session_expires_at"] is not None
    assert session_state["session_state"] == "LOGGED_IN"

    # Verify expiry time is reasonable
    time_diff = session_state["session_expires_at"] - datetime.now(tz=UTC)
    assert timedelta(minutes=55) < time_diff < timedelta(minutes=65)


@pytest.mark.asyncio
async def test_session_manager_login_integration(session_connector, user_credentials):
    """
    Test SessionManager login method with mocked HTTP.

    Verifies that SessionManager correctly:
    1. Builds login request
    2. Extracts session token
    3. Calculates expiry
    4. Returns correct session state
    """
    # Mock HTTP response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"token": "integration-test-token"}
    mock_response.headers = {"X-Auth-Token": "integration-test-token"}
    mock_response.cookies = {}

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client

        # Perform login
        session_manager = SessionManager()
        token, expires_at, state = await session_manager.login(
            connector=session_connector, credentials=user_credentials
        )

        # Verify results
        assert token == "integration-test-token"
        assert expires_at > datetime.now(tz=UTC)
        assert expires_at < datetime.now(tz=UTC) + timedelta(seconds=3700)
        assert state == "LOGGED_IN"

        # Verify HTTP request was made correctly
        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[1]["method"] == "POST"
        assert "/api/v1/auth/login" in call_args[1]["url"]
        assert call_args[1]["json"] == {"username": "test_user", "password": "test_password"}
