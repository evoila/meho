# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_openapi/http_client.py

Tests HTTP request handling, auth, and error cases.
Goal: Increase coverage from 25% to 80%+
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from meho_app.core.errors import UpstreamApiError
from meho_app.modules.connectors.rest.http_client import GenericHTTPClient
from meho_app.modules.connectors.rest.schemas import EndpointDescriptor
from meho_app.modules.connectors.schemas import Connector


@pytest.fixture
def http_client():
    """Create HTTP client instance"""
    return GenericHTTPClient(timeout=30.0)


@pytest.fixture
def sample_api_key_connector():
    """Sample connector with API key auth"""
    now = datetime.now(tz=UTC)
    return Connector(
        id="test-connector-1",
        tenant_id="test-tenant",
        name="Test API",
        description="Test API connector",
        base_url="https://api.example.com",
        auth_type="API_KEY",
        auth_config={"header_name": "X-API-Key", "api_key": "test-api-key-123"},
        is_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_basic_auth_connector():
    """Sample connector with basic auth"""
    now = datetime.now(tz=UTC)
    return Connector(
        id="test-connector-2",
        tenant_id="test-tenant",
        name="Test API Basic",
        description="Test API with basic auth",
        base_url="https://api.example.com",
        auth_type="BASIC",
        auth_config={"username": "testuser", "password": "testpass"},
        is_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_oauth2_connector():
    """Sample connector with OAuth2"""
    now = datetime.now(tz=UTC)
    return Connector(
        id="test-connector-3",
        tenant_id="test-tenant",
        name="Test API OAuth2",
        description="Test API with OAuth2",
        base_url="https://api.example.com",
        auth_type="OAUTH2",
        auth_config={"access_token": "test-token-123"},
        is_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_endpoint():
    """Sample endpoint descriptor"""
    now = datetime.now(tz=UTC)
    return EndpointDescriptor(
        id="test-endpoint-1",
        connector_id="test-connector-1",
        method="GET",
        path="/api/v1/users/{user_id}",
        summary="Get user by ID",
        description="Retrieve user details",
        created_at=now,
    )


# ============================================================================
# Tests for call_endpoint()
# ============================================================================


class TestCallEndpoint:
    """Tests for call_endpoint method"""

    @pytest.mark.asyncio
    async def test_call_endpoint_success_json_response(
        self, http_client, sample_api_key_connector, sample_endpoint
    ):
        """Test successful API call with JSON response"""
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "123", "name": "Test User"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act
            status, data = await http_client.call_endpoint(
                connector=sample_api_key_connector,
                endpoint=sample_endpoint,
                path_params={"user_id": "123"},
            )

            # Assert
            assert status == 200
            assert data == {"id": "123", "name": "Test User"}
            mock_client.request.assert_called_once()

            # Verify URL construction
            call_args = mock_client.request.call_args
            assert call_args.kwargs["url"] == "https://api.example.com/api/v1/users/123"
            assert call_args.kwargs["method"] == "GET"

    @pytest.mark.asyncio
    async def test_call_endpoint_with_query_params(self, http_client, sample_api_key_connector):
        """Test API call with query parameters"""
        # Arrange
        endpoint = EndpointDescriptor(
            id="test-endpoint",
            connector_id="test-connector",
            method="GET",
            path="/api/v1/users",
            summary="List users",
            description="Get all users",
            created_at=datetime.now(tz=UTC),
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"users": []}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act
            status, _data = await http_client.call_endpoint(
                connector=sample_api_key_connector,
                endpoint=endpoint,
                query_params={"limit": 10, "offset": 20},
            )

            # Assert
            assert status == 200
            call_args = mock_client.request.call_args
            assert call_args.kwargs["params"] == {"limit": 10, "offset": 20}

    @pytest.mark.asyncio
    async def test_call_endpoint_with_body(self, http_client, sample_api_key_connector):
        """Test POST request with body"""
        # Arrange
        endpoint = EndpointDescriptor(
            id="test-endpoint",
            connector_id="test-connector",
            method="POST",
            path="/api/v1/users",
            summary="Create user",
            description="Create a new user",
            created_at=datetime.now(tz=UTC),
        )

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "456", "name": "New User"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act
            status, data = await http_client.call_endpoint(
                connector=sample_api_key_connector,
                endpoint=endpoint,
                body={"name": "New User", "email": "new@example.com"},
            )

            # Assert
            assert status == 201
            assert data["id"] == "456"
            call_args = mock_client.request.call_args
            assert call_args.kwargs["json"] == {"name": "New User", "email": "new@example.com"}

    @pytest.mark.asyncio
    async def test_call_endpoint_text_response(
        self, http_client, sample_api_key_connector, sample_endpoint
    ):
        """Test API call with text response (non-JSON)"""
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Not JSON")
        mock_response.text = "Plain text response"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act
            status, data = await http_client.call_endpoint(
                connector=sample_api_key_connector,
                endpoint=sample_endpoint,
                path_params={"user_id": "123"},
            )

            # Assert
            assert status == 200
            assert data == "Plain text response"

    @pytest.mark.asyncio
    async def test_call_endpoint_4xx_error(
        self, http_client, sample_api_key_connector, sample_endpoint
    ):
        """Test API call with 4xx error"""
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"error": "Not found"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act & Assert
            with pytest.raises(UpstreamApiError) as exc_info:
                await http_client.call_endpoint(
                    connector=sample_api_key_connector,
                    endpoint=sample_endpoint,
                    path_params={"user_id": "999"},
                )

            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_call_endpoint_5xx_error(
        self, http_client, sample_api_key_connector, sample_endpoint
    ):
        """Test API call with 5xx error"""
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"error": "Internal server error"}

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act & Assert
            with pytest.raises(UpstreamApiError) as exc_info:
                await http_client.call_endpoint(
                    connector=sample_api_key_connector,
                    endpoint=sample_endpoint,
                    path_params={"user_id": "123"},
                )

            assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_call_endpoint_timeout(
        self, http_client, sample_api_key_connector, sample_endpoint
    ):
        """Test API call timeout"""
        # Arrange
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act & Assert
            with pytest.raises(UpstreamApiError) as exc_info:
                await http_client.call_endpoint(
                    connector=sample_api_key_connector,
                    endpoint=sample_endpoint,
                    path_params={"user_id": "123"},
                )

            assert exc_info.value.status_code == 504
            assert "timeout" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_call_endpoint_request_error(
        self, http_client, sample_api_key_connector, sample_endpoint
    ):
        """Test API call with network error"""
        # Arrange
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.request = AsyncMock(side_effect=httpx.RequestError("Connection failed"))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Act & Assert
            with pytest.raises(UpstreamApiError) as exc_info:
                await http_client.call_endpoint(
                    connector=sample_api_key_connector,
                    endpoint=sample_endpoint,
                    path_params={"user_id": "123"},
                )

            assert exc_info.value.status_code == 503
            assert "Request failed" in str(exc_info.value)


# ============================================================================
# Tests for _build_url()
# ============================================================================


class TestBuildUrl:
    """Tests for _build_url method"""

    def test_build_url_with_path_params(self, http_client):
        """Test URL building with path parameter substitution"""
        # Act
        url = http_client._build_url(
            base_url="https://api.example.com",
            path="/api/v1/users/{user_id}/posts/{post_id}",
            path_params={"user_id": "123", "post_id": "456"},
        )

        # Assert
        assert url == "https://api.example.com/api/v1/users/123/posts/456"

    def test_build_url_without_path_params(self, http_client):
        """Test URL building without path parameters"""
        # Act
        url = http_client._build_url(
            base_url="https://api.example.com", path="/api/v1/users", path_params={}
        )

        # Assert
        assert url == "https://api.example.com/api/v1/users"

    def test_build_url_missing_path_param(self, http_client):
        """Test URL building with missing path parameter"""
        # Act & Assert
        with pytest.raises(ValueError) as exc_info:  # noqa: PT011 -- test validates exception type is sufficient
            http_client._build_url(
                base_url="https://api.example.com", path="/api/v1/users/{user_id}", path_params={}
            )

        assert "Missing required path parameters" in str(exc_info.value)

    def test_build_url_with_base_url_trailing_slash(self, http_client):
        """Test URL building with trailing slash in base URL"""
        # Act
        url = http_client._build_url(
            base_url="https://api.example.com/", path="/api/v1/users", path_params={}
        )

        # Assert
        assert url == "https://api.example.com/api/v1/users"


# ============================================================================
# Tests for _build_headers()
# ============================================================================


class TestBuildHeaders:
    """Tests for _build_headers method"""

    def test_build_headers_api_key_default_header(self, http_client, sample_api_key_connector):
        """Test header building with API key auth (default header name)"""
        # Act
        headers = http_client._build_headers(sample_api_key_connector)

        # Assert
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"
        assert headers["X-API-Key"] == "test-api-key-123"

    def test_build_headers_api_key_custom_header(self, http_client):
        """Test header building with custom API key header"""
        # Arrange
        now = datetime.now(tz=UTC)
        connector = Connector(
            id="test",
            tenant_id="test-tenant",
            name="Test",
            description="Test",
            base_url="https://api.example.com",
            auth_type="API_KEY",
            auth_config={"header_name": "Authorization", "api_key": "custom-key-456"},
            is_active=True,
            created_at=now,
            updated_at=now,
        )

        # Act
        headers = http_client._build_headers(connector)

        # Assert
        assert headers["Authorization"] == "custom-key-456"

    def test_build_headers_basic_auth(self, http_client, sample_basic_auth_connector):
        """Test header building with basic auth"""
        # Act
        headers = http_client._build_headers(sample_basic_auth_connector)

        # Assert
        assert "Authorization" in headers
        assert headers["Authorization"].startswith("Basic ")

        # Verify base64 encoding
        import base64

        encoded = headers["Authorization"].replace("Basic ", "")
        decoded = base64.b64decode(encoded).decode()
        assert decoded == "testuser:testpass"

    def test_build_headers_oauth2(self, http_client, sample_oauth2_connector):
        """Test header building with OAuth2"""
        # Act
        headers = http_client._build_headers(sample_oauth2_connector)

        # Assert
        assert headers["Authorization"] == "Bearer test-token-123"

    def test_build_headers_user_provided_credentials(self, http_client, sample_api_key_connector):
        """Test header building with user-provided credentials"""
        # Arrange
        user_credentials = {"header_name": "X-API-Key", "api_key": "user-specific-key"}

        # Act
        headers = http_client._build_headers(
            sample_api_key_connector, user_credentials=user_credentials
        )

        # Assert
        assert headers["X-API-Key"] == "user-specific-key"

    def test_build_headers_no_auth_config(self, http_client):
        """Test header building without auth config"""
        # Arrange
        now = datetime.now(tz=UTC)
        connector = Connector(
            id="test",
            tenant_id="test-tenant",
            name="Test",
            description="Test",
            base_url="https://api.example.com",
            auth_type="API_KEY",
            auth_config={},  # Empty auth config
            is_active=True,
            created_at=now,
            updated_at=now,
        )

        # Act
        headers = http_client._build_headers(connector)

        # Assert
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept"] == "application/json"
        # No API key header should be added
        assert "X-API-Key" not in headers


# ============================================================================
# Tests for client initialization
# ============================================================================


class TestClientInit:
    """Tests for client initialization"""

    def test_init_default_timeout(self):
        """Test client initialization with default timeout"""
        # Act
        client = GenericHTTPClient()

        # Assert
        assert client.timeout == pytest.approx(30.0)

    def test_init_custom_timeout(self):
        """Test client initialization with custom timeout"""
        # Act
        client = GenericHTTPClient(timeout=60.0)

        # Assert
        assert client.timeout == pytest.approx(60.0)
