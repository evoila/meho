# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Session manager for handling session-based authentication.

Manages login, session token storage, expiry tracking, and auto-login.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import httpx

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.schemas import Connector

logger = get_logger(__name__)


class SessionManager:
    """Manages session-based authentication for REST connectors."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    async def login(
        self,
        connector: Connector,
        credentials: dict[str, str],
        session_token: str | None = None,
        session_expires_at: datetime | None = None,
        refresh_token: str | None = None,
        refresh_expires_at: datetime | None = None,
    ) -> tuple[str, str | None, datetime, datetime | None, str]:
        """
        Login to get a session token (and optionally refresh token).

        Args:
            connector: Connector configuration with login_url and login_config
            credentials: User credentials (username, password, etc.)
            session_token: Current session token (if any)
            session_expires_at: Current session expiry (if any)
            refresh_token: Current refresh token (if any)
            refresh_expires_at: Current refresh expiry (if any)

        Returns:
            (session_token, refresh_token, session_expires_at, refresh_expires_at, session_state)
        """
        # Check if we can reuse existing token
        if session_token and session_expires_at:
            now = datetime.now(tz=UTC)
            if session_expires_at > now + timedelta(minutes=5):
                logger.info(
                    f"Reusing valid session token (expires in {(session_expires_at - now).total_seconds():.0f}s)"
                )
                return (
                    session_token,
                    refresh_token,
                    session_expires_at,
                    refresh_expires_at,
                    "LOGGED_IN",
                )

            # Try to refresh if possible
            if (
                refresh_token
                and connector.login_config
                and connector.login_config.get("refresh_url")
            ) and (not refresh_expires_at or refresh_expires_at > now):
                try:
                    new_session_token, new_expires_at = await self.refresh(connector, refresh_token)
                    return (
                        new_session_token,
                        refresh_token,
                        new_expires_at,
                        refresh_expires_at,
                        "LOGGED_IN",
                    )
                except Exception as e:
                    logger.warning(f"Refresh failed: {e}, performing full login")

        # Validate connector configuration
        if connector.auth_type != "SESSION":
            raise ValueError(f"Connector auth_type must be SESSION, got: {connector.auth_type}")

        if not connector.login_url:
            raise ValueError("Connector login_url is required for SESSION auth")

        if not connector.login_config:
            raise ValueError("Connector login_config is required for SESSION auth")

        # Build login URL
        login_url = urljoin(connector.base_url, connector.login_url.lstrip("/"))

        # Determine login auth type
        login_auth_type = connector.login_config.get("login_auth_type", "body")

        # Build login headers
        login_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        custom_headers = connector.login_config.get("login_headers", {})
        if custom_headers:
            login_headers.update(custom_headers)

        # Make login request
        login_method = (connector.login_method or "POST").upper()

        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:  # noqa: S501 -- internal service, self-signed cert
            try:
                if login_auth_type == "basic":
                    username = credentials.get("username", "")
                    password = credentials.get("password", "")

                    response = await client.request(
                        method=login_method,
                        url=login_url,
                        auth=(username, password),
                        headers=login_headers,
                    )
                else:
                    login_body = self._build_login_body(connector.login_config, credentials)

                    response = await client.request(
                        method=login_method,
                        url=login_url,
                        json=login_body if login_method == "POST" else None,
                        params=login_body if login_method == "GET" else None,
                        headers=login_headers,
                    )

                if response.status_code >= 400:
                    error_text = response.text
                    raise ValueError(f"Login failed: {response.status_code} - {error_text}")

                # Parse response
                try:
                    response_data = response.json()
                except:  # noqa: E722 -- intentional bare except for cleanup
                    response_data = {"text": response.text}

                # Extract session token
                session_token = self._extract_session_token(
                    connector.login_config, response_data, response.headers, response.cookies
                )

                if not session_token:
                    raise ValueError("Could not extract session token from login response")

                # Extract refresh token (optional)
                refresh_token = None
                refresh_expires_at = None
                if connector.login_config.get("refresh_token_path"):
                    refresh_token = self._extract_refresh_token(
                        connector.login_config, response_data
                    )
                    if refresh_token:
                        refresh_duration = connector.login_config.get("refresh_token_expires_in")
                        if refresh_duration:
                            refresh_expires_at = datetime.now(tz=UTC) + timedelta(
                                seconds=refresh_duration
                            )

                # Calculate session expiry
                session_duration = connector.login_config.get("session_duration_seconds", 3600)
                expires_at = datetime.now(tz=UTC) + timedelta(seconds=session_duration)

                return session_token, refresh_token, expires_at, refresh_expires_at, "LOGGED_IN"

            except httpx.TimeoutException as e:
                raise ValueError(f"Login timeout: {e}") from e
            except httpx.RequestError as e:
                raise ValueError(f"Login request failed: {e}") from e

    def _build_login_body(
        self, login_config: dict[str, Any], credentials: dict[str, str]
    ) -> dict[str, Any]:
        """Build login request body from template."""
        body_template = login_config.get("body_template", {})
        login_body = {}

        for key, value in body_template.items():
            if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
                var_name = value[2:-2].strip()
                login_body[key] = credentials.get(var_name, value)
            else:
                login_body[key] = value

        return login_body

    def _extract_session_token(
        self,
        login_config: dict[str, Any],
        response_data: dict[str, Any],
        headers: Any,
        cookies: Any,
    ) -> str | None:
        """Extract session token from login response."""
        token_location = login_config.get("token_location", "header")
        token_name = login_config.get("token_name", "X-Auth-Token")

        if token_location == "header":  # noqa: S105 -- configuration default, not a secret
            token = headers.get(token_name)
            if token:
                return str(token)

        elif token_location == "cookie":  # noqa: S105 -- configuration default, not a secret
            token = cookies.get(token_name)
            if token:
                return str(token)

        elif token_location == "body":  # noqa: S105 -- configuration default, not a secret
            token_path = login_config.get("token_path", f"$.{token_name}")
            token = self._jsonpath_extract(response_data, token_path)
            if token:
                return str(token)

        return None

    def _extract_refresh_token(
        self, login_config: dict[str, Any], response_data: dict[str, Any]
    ) -> str | None:
        """Extract refresh token from login response."""
        refresh_token_path = login_config.get("refresh_token_path")
        if not refresh_token_path:
            return None

        token = self._jsonpath_extract(response_data, refresh_token_path)
        if token:
            return str(token)

        return None

    def _jsonpath_extract(self, data: Any, path: str) -> Any | None:
        """Simple JSONPath extraction (supports $.key and $.key.subkey)."""
        if not path.startswith("$."):
            return None

        keys = path[2:].split(".")
        current = data

        for key in keys:
            if isinstance(current, dict):
                current = current.get(key)
                if current is None:
                    return None
            else:
                return None

        return current

    async def refresh(self, connector: Connector, refresh_token: str) -> tuple[str, datetime]:
        """Refresh access token using refresh token."""
        if not connector.login_config:
            raise ValueError("Connector login_config is required for refresh")

        refresh_url = connector.login_config.get("refresh_url")
        if not refresh_url:
            raise ValueError("Connector does not support token refresh")

        full_refresh_url = urljoin(connector.base_url, refresh_url.lstrip("/"))
        refresh_body = self._build_refresh_body(connector.login_config, refresh_token)
        refresh_method = connector.login_config.get("refresh_method", "POST").upper()

        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:  # noqa: S501 -- internal service, self-signed cert
            try:
                response = await client.request(
                    method=refresh_method,
                    url=full_refresh_url,
                    json=refresh_body if refresh_method in ["POST", "PATCH", "PUT"] else None,
                    params=refresh_body if refresh_method == "GET" else None,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )

                if response.status_code >= 400:
                    raise ValueError(f"Token refresh failed: {response.status_code}")

                try:
                    response_data = response.json()
                except:  # noqa: E722 -- intentional bare except for cleanup
                    response_data = {"text": response.text}

                new_token = self._extract_session_token(
                    connector.login_config, response_data, response.headers, response.cookies
                )

                if not new_token:
                    raise ValueError("Could not extract new token from refresh response")

                session_duration = connector.login_config.get("session_duration_seconds", 3600)
                new_expires_at = datetime.now(tz=UTC) + timedelta(seconds=session_duration)

                return new_token, new_expires_at

            except httpx.TimeoutException as e:
                raise ValueError(f"Token refresh timeout: {e}") from e
            except httpx.RequestError as e:
                raise ValueError(f"Token refresh failed: {e}") from e

    def _build_refresh_body(
        self, login_config: dict[str, Any], refresh_token: str
    ) -> dict[str, Any]:
        """Build refresh request body from template."""
        body_template = login_config.get("refresh_body_template", {})
        if not body_template:
            return {"refresh_token": refresh_token}

        result = self._replace_template_vars(body_template, {"refresh_token": refresh_token})
        return result if isinstance(result, dict) else {"refresh_token": refresh_token}

    def _replace_template_vars(self, template: Any, vars: dict[str, str]) -> Any:
        """Recursively replace template variables in a data structure."""
        if isinstance(template, dict):
            return {k: self._replace_template_vars(v, vars) for k, v in template.items()}
        elif isinstance(template, list):
            return [self._replace_template_vars(item, vars) for item in template]
        elif isinstance(template, str):
            if template.startswith("{{") and template.endswith("}}"):
                var_name = template[2:-2].strip()
                return vars.get(var_name, template)
            return template
        else:
            return template

    def build_auth_headers(self, connector: Connector, session_token: str) -> dict[str, str]:
        """Build authentication headers with session token."""
        if not connector.login_config:
            return {"Authorization": f"Bearer {session_token}"}

        header_name = connector.login_config.get("header_name")

        if header_name:
            return {header_name: session_token}
        elif connector.login_config.get(
            "token_location"
        ) == "header" and connector.login_config.get("token_name"):
            return {connector.login_config["token_name"]: session_token}
        else:
            return {"Authorization": f"Bearer {session_token}"}
