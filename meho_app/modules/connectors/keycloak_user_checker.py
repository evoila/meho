# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Keycloak user active status checker with caching.

Used by CredentialResolver to validate that delegating users are still
active before using their delegated credentials in automated sessions.

Fails open: if Keycloak is unreachable, assumes the user is active
to prevent Keycloak outages from breaking all automated sessions.
"""

from __future__ import annotations

import time

from keycloak import KeycloakAdmin

from meho_app.core.otel import get_logger

logger = get_logger(__name__)


class KeycloakUserChecker:
    """Check Keycloak user active status with TTL-based caching."""

    CACHE_TTL_SECONDS = 300  # 5 minutes

    def __init__(self, keycloak_url: str, admin_username: str, admin_password: str) -> None:
        self._cache: dict[str, tuple[bool, float]] = {}
        self._keycloak_url = keycloak_url
        self._admin_username = admin_username
        self._admin_password = admin_password

    def is_user_active(self, user_email: str, realm: str | None = None) -> bool:
        """
        Check if a user is active (enabled) in Keycloak.

        Uses a TTL cache to avoid excessive Keycloak calls. Fails open:
        returns True if Keycloak is unreachable, so that Keycloak outages
        do not break all automated sessions.

        Args:
            user_email: User email or identifier to look up.
            realm: Keycloak realm (defaults to "master").

        Returns:
            True if user is active or Keycloak is unreachable, False if disabled.
        """
        cache_key = f"{realm or 'default'}:{user_email}"
        now = time.time()

        if cache_key in self._cache:
            is_active, expires_at = self._cache[cache_key]
            if now < expires_at:
                return is_active

        try:
            admin = KeycloakAdmin(
                server_url=self._keycloak_url,
                username=self._admin_username,
                password=self._admin_password,
                realm_name=realm or "master",
            )
            kc_user_id = admin.get_user_id(user_email)
            if not kc_user_id:
                self._cache[cache_key] = (False, now + self.CACHE_TTL_SECONDS)
                return False
            user_repr = admin.get_user(kc_user_id)
            is_active = user_repr.get("enabled", False)
        except Exception:
            # Fail-open: Keycloak outage should not break all automated sessions
            logger.warning(f"Keycloak user check failed for {user_email}, assuming active")
            is_active = True

        self._cache[cache_key] = (is_active, now + self.CACHE_TTL_SECONDS)
        return is_active

    def clear_cache(self) -> None:
        """Clear the entire user status cache."""
        self._cache.clear()
