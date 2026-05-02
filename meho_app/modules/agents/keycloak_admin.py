# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Keycloak Tenant Manager for realm lifecycle management.

TASK-139 Phase 4: Tenant Management API

Provides CRUD operations for Keycloak realms:
- Create realm for new tenant
- Disable/enable realm
- Get realm information

Note: This class uses synchronous python-keycloak under the hood.
For async contexts, consider running in a thread pool if needed.
"""

from functools import lru_cache

from keycloak import KeycloakAdmin
from keycloak.exceptions import KeycloakError, KeycloakGetError

from meho_app.api.config import get_api_config
from meho_app.core.otel import get_logger

logger = get_logger(__name__)


# Default roles to create in every new tenant realm
DEFAULT_TENANT_ROLES = ["admin", "user", "viewer"]


class KeycloakTenantManager:
    """
    Manage Keycloak realms for tenant lifecycle.

    Each MEHO tenant maps to a Keycloak realm. This class handles:
    - Creating new realms with default roles
    - Disabling/enabling realms (soft delete)
    - Querying realm information
    """

    def __init__(
        self,
        server_url: str,
        admin_username: str,
        admin_password: str,
    ) -> None:
        """
        Initialize Keycloak admin client.

        Args:
            server_url: Keycloak server URL (e.g., http://localhost:8080)
            admin_username: Admin username for master realm
            admin_password: Admin password for master realm
        """
        self.server_url = server_url
        self._admin_username = admin_username
        self._admin_password = admin_password
        self._admin: KeycloakAdmin | None = None

    def _get_admin(self) -> KeycloakAdmin:
        """Get or create KeycloakAdmin instance (lazy initialization)."""
        if self._admin is None:
            self._admin = KeycloakAdmin(
                server_url=self.server_url,
                username=self._admin_username,
                password=self._admin_password,
                realm_name="master",
                verify=True,
            )
        return self._admin

    def create_realm(
        self,
        tenant_id: str,
        display_name: str,
        enabled: bool = True,
    ) -> dict:
        """
        Create a new Keycloak realm for a tenant.

        Args:
            tenant_id: Unique tenant identifier (becomes realm name)
            display_name: Human-readable display name
            enabled: Whether the realm is enabled (default True)

        Returns:
            dict with realm creation details

        Raises:
            KeycloakError: If realm creation fails
        """
        admin = self._get_admin()

        # Create realm payload
        realm_payload = {
            "realm": tenant_id,
            "displayName": display_name,
            "enabled": enabled,
            "registrationAllowed": False,  # Controlled registration
            "loginWithEmailAllowed": True,
            "duplicateEmailsAllowed": False,
            "resetPasswordAllowed": True,
            "editUsernameAllowed": False,
            "bruteForceProtected": True,
            "sslRequired": "external",  # Require SSL in production
            # Token settings
            "accessTokenLifespan": 300,  # 5 minutes
            "ssoSessionIdleTimeout": 1800,  # 30 minutes
            "ssoSessionMaxLifespan": 36000,  # 10 hours
            # OIDC settings for MEHO frontend
            "accessCodeLifespan": 60,
            "accessCodeLifespanLogin": 1800,
            "accessCodeLifespanUserAction": 300,
        }

        try:
            admin.create_realm(realm_payload)
            logger.info(f"Created Keycloak realm: {tenant_id}")

            # Create default roles in the new realm
            self._create_default_roles(tenant_id)

            # Create MEHO frontend client
            self._create_frontend_client(tenant_id)

            return {
                "realm": tenant_id,
                "display_name": display_name,
                "enabled": enabled,
                "roles_created": DEFAULT_TENANT_ROLES,
            }

        except KeycloakError as e:
            logger.error(f"Failed to create realm {tenant_id}: {e}")
            raise

    def _create_default_roles(self, realm_name: str) -> None:
        """Create default roles in a realm."""
        admin = self._get_admin()

        # Switch to the new realm
        admin.connection.realm_name = realm_name

        for role_name in DEFAULT_TENANT_ROLES:
            try:
                admin.create_realm_role(
                    {
                        "name": role_name,
                        "description": f"MEHO {role_name} role",
                    }
                )
                logger.debug(f"Created role {role_name} in realm {realm_name}")
            except KeycloakError as e:
                # Role might already exist
                logger.warning(f"Could not create role {role_name}: {e}")

        # Switch back to master realm
        admin.connection.realm_name = "master"

    def _create_frontend_client(self, realm_name: str) -> None:
        """Create OIDC client for MEHO frontend in the realm."""
        admin = self._get_admin()

        # Switch to the new realm
        admin.connection.realm_name = realm_name

        client_payload = {
            "clientId": "meho-frontend",
            "name": "MEHO Frontend",
            "enabled": True,
            "publicClient": True,  # SPA client (no secret)
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "protocol": "openid-connect",
            # Redirect URIs - should be configured per environment
            "redirectUris": [
                "http://localhost:5173/*",  # Dev
                "http://localhost:3000/*",  # Alternative dev
            ],
            "webOrigins": [
                "http://localhost:5173",
                "http://localhost:3000",
            ],
            # Include roles in access token
            "attributes": {
                "access.token.claim": "true",
            },
        }

        try:
            admin.create_client(client_payload)
            logger.debug(f"Created meho-frontend client in realm {realm_name}")
        except KeycloakError as e:
            logger.warning(f"Could not create frontend client: {e}")

        # Switch back to master realm
        admin.connection.realm_name = "master"

    def disable_realm(self, tenant_id: str) -> None:
        """
        Disable a Keycloak realm (soft delete).

        Users will not be able to log in to a disabled realm.

        Args:
            tenant_id: The realm/tenant to disable

        Raises:
            KeycloakError: If operation fails
        """
        admin = self._get_admin()

        try:
            admin.update_realm(tenant_id, {"enabled": False})
            logger.info(f"Disabled Keycloak realm: {tenant_id}")
        except KeycloakError as e:
            logger.error(f"Failed to disable realm {tenant_id}: {e}")
            raise

    def enable_realm(self, tenant_id: str) -> None:
        """
        Re-enable a disabled Keycloak realm.

        Args:
            tenant_id: The realm/tenant to enable

        Raises:
            KeycloakError: If operation fails
        """
        admin = self._get_admin()

        try:
            admin.update_realm(tenant_id, {"enabled": True})
            logger.info(f"Enabled Keycloak realm: {tenant_id}")
        except KeycloakError as e:
            logger.error(f"Failed to enable realm {tenant_id}: {e}")
            raise

    def get_realm_info(self, tenant_id: str) -> dict | None:
        """
        Get information about a Keycloak realm.

        Args:
            tenant_id: The realm/tenant to query

        Returns:
            Realm information dict, or None if realm doesn't exist
        """
        admin = self._get_admin()

        try:
            # Get all realms and find the matching one
            realms = admin.get_realms()
            for realm in realms:
                if realm.get("realm") == tenant_id:
                    return {
                        "realm": realm.get("realm"),
                        "display_name": realm.get("displayName"),
                        "enabled": realm.get("enabled", True),
                    }
            return None
        except KeycloakGetError:
            return None
        except KeycloakError as e:
            logger.error(f"Failed to get realm info for {tenant_id}: {e}")
            raise

    def realm_exists(self, tenant_id: str) -> bool:
        """
        Check if a Keycloak realm exists.

        Args:
            tenant_id: The realm/tenant to check

        Returns:
            True if realm exists, False otherwise
        """
        return self.get_realm_info(tenant_id) is not None

    def delete_realm(self, tenant_id: str) -> None:
        """
        Permanently delete a Keycloak realm.

        WARNING: This is irreversible. Prefer disable_realm for soft delete.

        Args:
            tenant_id: The realm/tenant to delete

        Raises:
            KeycloakError: If operation fails
        """
        admin = self._get_admin()

        try:
            admin.delete_realm(tenant_id)
            logger.info(f"Deleted Keycloak realm: {tenant_id}")
        except KeycloakError as e:
            logger.error(f"Failed to delete realm {tenant_id}: {e}")
            raise


@lru_cache(maxsize=1)
def get_keycloak_manager() -> KeycloakTenantManager:
    """
    Get a singleton KeycloakTenantManager instance.

    Uses configuration from API config.

    Returns:
        KeycloakTenantManager instance
    """
    config = get_api_config()
    return KeycloakTenantManager(
        server_url=config.keycloak_url,
        admin_username=config.keycloak_admin_username,
        admin_password=config.keycloak_admin_password,
    )
