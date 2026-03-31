"""httpx Auth strategy classes for connector authentication.

Produces httpx.Auth objects from AuthConfig + credentials dict.
OAuth2 client_credentials is handled in RESTConnector directly (needs token
refresh lifecycle), so build_auth raises ValueError for it.
"""

from __future__ import annotations

from typing import Generator
from urllib.parse import urlencode

import httpx

from meho_claude.core.connectors.models import AuthConfig


class BearerAuth(httpx.Auth):
    """Bearer token authentication for httpx."""

    def __init__(self, token: str) -> None:
        self.token = token

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class APIKeyAuth(httpx.Auth):
    """API key authentication — in header (default) or query parameter."""

    def __init__(
        self,
        api_key: str,
        header_name: str = "X-API-Key",
        in_query: bool = False,
        query_param: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.header_name = header_name
        self.in_query = in_query
        self.query_param = query_param or "api_key"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if self.in_query:
            # Append API key as query parameter
            url = str(request.url)
            separator = "&" if "?" in url else "?"
            new_url = f"{url}{separator}{urlencode({self.query_param: self.api_key})}"
            request.url = httpx.URL(new_url)
        else:
            request.headers[self.header_name] = self.api_key
        yield request


def build_auth(auth_config: AuthConfig, credentials: dict) -> httpx.Auth:
    """Factory: build the correct httpx.Auth from config + credentials.

    Args:
        auth_config: AuthConfig from connector YAML.
        credentials: Decrypted credential dict from CredentialManager.

    Returns:
        httpx.Auth instance ready for use with httpx.AsyncClient.

    Raises:
        ValueError: For oauth2_client_credentials (handled in RESTConnector)
                    or truly unsupported methods.
    """
    method = auth_config.method

    if method == "bearer":
        return BearerAuth(token=credentials["token"])

    if method == "basic":
        return httpx.BasicAuth(
            username=credentials["username"],
            password=credentials["password"],
        )

    if method == "api_key":
        return APIKeyAuth(
            api_key=credentials["api_key"],
            header_name=auth_config.header_name or "X-API-Key",
            in_query=auth_config.in_query,
            query_param=auth_config.query_param,
        )

    if method == "oauth2_client_credentials":
        raise ValueError(
            "OAuth2 client_credentials auth is handled by the RESTConnector directly, "
            "not via build_auth. Use RESTConnector's built-in OAuth2 flow."
        )

    raise ValueError(f"Unsupported auth method: {method!r}")
