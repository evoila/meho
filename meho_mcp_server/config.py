# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Configuration for MEHO MCP Server."""

import os
import time
from typing import Optional

import httpx

# Token cache
_token_cache: dict = {"token": None, "expires_at": 0}


def get_meho_api_url() -> str:
    """Get the MEHO API base URL from environment."""
    return os.getenv("MEHO_API_URL", "http://localhost:8000")


def get_keycloak_url() -> str:
    """Get the Keycloak URL from environment."""
    return os.getenv("MEHO_KEYCLOAK_URL", "http://localhost:8080")


def get_auth_token() -> Optional[str]:
    """
    Get the authentication token for MEHO API calls.
    
    Tries in order:
    1. Static token from MEHO_AUTH_TOKEN env var
    2. Fresh token from Keycloak using admin credentials
    """
    # Check for static token first
    static_token = os.getenv("MEHO_AUTH_TOKEN")
    if static_token:
        return static_token
    
    # Try to get/refresh token from Keycloak
    return _get_keycloak_token()


def _get_keycloak_token() -> Optional[str]:
    """Get a fresh token from Keycloak using admin credentials."""
    global _token_cache
    
    # Return cached token if still valid (with 30 second buffer)
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["token"]
    
    keycloak_url = get_keycloak_url()
    token_url = f"{keycloak_url}/realms/master/protocol/openid-connect/token"
    
    # Use admin credentials (for local dev only)
    username = os.getenv("MEHO_KEYCLOAK_USER", "admin")
    password = os.getenv("MEHO_KEYCLOAK_PASSWORD", "admin")
    
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                token_url,
                data={
                    "client_id": "admin-cli",
                    "username": username,
                    "password": password,
                    "grant_type": "password",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if response.status_code == 200:
                data = response.json()
                token = data.get("access_token")
                expires_in = data.get("expires_in", 60)
                _token_cache["token"] = token
                _token_cache["expires_at"] = time.time() + expires_in
                return token
    except Exception:
        # Silently fail - auth is optional, API may work without it
        pass
    
    return None


def get_auth_headers() -> dict:
    """Get authentication headers for MEHO API calls."""
    headers = {"Content-Type": "application/json"}
    token = get_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
