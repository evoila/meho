"""Tests for httpx auth strategy classes."""

import httpx
import pytest

from meho_claude.core.connectors.auth import APIKeyAuth, BearerAuth, build_auth
from meho_claude.core.connectors.models import AuthConfig


class TestBearerAuth:
    def test_adds_authorization_header(self):
        auth = BearerAuth(token="my-secret-token")
        request = httpx.Request("GET", "https://api.example.com/test")

        # auth_flow is a generator yielding modified requests
        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["Authorization"] == "Bearer my-secret-token"


class TestAPIKeyAuth:
    def test_adds_key_in_header_default(self):
        auth = APIKeyAuth(api_key="key123")
        request = httpx.Request("GET", "https://api.example.com/test")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["X-API-Key"] == "key123"

    def test_adds_key_in_custom_header(self):
        auth = APIKeyAuth(api_key="key123", header_name="X-Custom-Key")
        request = httpx.Request("GET", "https://api.example.com/test")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert modified_request.headers["X-Custom-Key"] == "key123"

    def test_adds_key_in_query_param(self):
        auth = APIKeyAuth(api_key="key123", in_query=True, query_param="api_key")
        request = httpx.Request("GET", "https://api.example.com/test")

        flow = auth.auth_flow(request)
        modified_request = next(flow)

        assert "api_key=key123" in str(modified_request.url)


class TestBuildAuth:
    def test_bearer(self):
        auth_config = AuthConfig(method="bearer", credential_name="my-api")
        credentials = {"token": "bearer-token-123"}

        result = build_auth(auth_config, credentials)
        assert isinstance(result, BearerAuth)

    def test_basic(self):
        auth_config = AuthConfig(method="basic", credential_name="my-api")
        credentials = {"username": "user", "password": "pass"}

        result = build_auth(auth_config, credentials)
        assert isinstance(result, httpx.BasicAuth)

    def test_api_key_header(self):
        auth_config = AuthConfig(
            method="api_key",
            credential_name="my-api",
            header_name="X-Custom-Key",
        )
        credentials = {"api_key": "key-value-123"}

        result = build_auth(auth_config, credentials)
        assert isinstance(result, APIKeyAuth)

    def test_api_key_query(self):
        auth_config = AuthConfig(
            method="api_key",
            credential_name="my-api",
            in_query=True,
            query_param="key",
        )
        credentials = {"api_key": "key-value-123"}

        result = build_auth(auth_config, credentials)
        assert isinstance(result, APIKeyAuth)

    def test_oauth2_raises_not_implemented(self):
        auth_config = AuthConfig(
            method="oauth2_client_credentials",
            credential_name="my-oauth",
            token_url="https://auth.example.com/token",
        )
        credentials = {"client_id": "id", "client_secret": "secret"}

        with pytest.raises(ValueError, match="OAuth2"):
            build_auth(auth_config, credentials)

    def test_unsupported_method_raises(self):
        """build_auth should raise for completely unknown methods."""
        # We have to bypass pydantic validation to get an unsupported method through
        auth_config = AuthConfig.__new__(AuthConfig)
        object.__setattr__(auth_config, "method", "ntlm")
        object.__setattr__(auth_config, "credential_name", "x")
        object.__setattr__(auth_config, "header_name", None)
        object.__setattr__(auth_config, "in_query", False)
        object.__setattr__(auth_config, "query_param", None)
        object.__setattr__(auth_config, "token_url", None)

        credentials = {}
        with pytest.raises(ValueError, match="Unsupported auth method"):
            build_auth(auth_config, credentials)
