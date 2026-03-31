# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Storage handler mixin (Phase 92).

Handlers for Azure Storage operations: storage accounts, blob containers,
managed disks (alias), account keys, and usage. Uses native async Azure SDK clients.
"""

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.helpers import (
    _extract_resource_group,
)
from meho_app.modules.connectors.azure.serializers import (
    serialize_azure_storage_account,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.azure.connector import AzureConnector

logger = get_logger(__name__)


class StorageHandlerMixin:
    """Mixin providing Azure Storage operation handlers.

    Covers storage accounts, blob containers, managed disk aliases,
    account keys, and usage stats. All methods use native async
    Azure SDK calls.
    """

    if TYPE_CHECKING:
        _storage_client: Any
        _subscription_id: str
        _resource_group_filter: str | None

    # =========================================================================
    # STORAGE ACCOUNT OPERATIONS
    # =========================================================================

    async def _handle_list_azure_storage_accounts(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List storage accounts.

        If resource_group is provided, lists accounts in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for account in self._storage_client.storage_accounts.list_by_resource_group(resource_group):
                results.append(serialize_azure_storage_account(account))
        else:
            async for account in self._storage_client.storage_accounts.list():
                results.append(serialize_azure_storage_account(account))

        return results

    async def _handle_get_azure_storage_account(
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get storage account details."""
        resource_group = params["resource_group"]
        account_name = params["account_name"]

        account = await self._storage_client.storage_accounts.get_properties(
            resource_group_name=resource_group,
            account_name=account_name,
        )
        return serialize_azure_storage_account(account)

    # =========================================================================
    # BLOB CONTAINER OPERATIONS
    # =========================================================================

    async def _handle_list_azure_blob_containers(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List blob containers in a storage account."""
        resource_group = params["resource_group"]
        account_name = params["account_name"]

        results: list[dict[str, Any]] = []
        async for container in self._storage_client.blob_containers.list(
            resource_group_name=resource_group,
            account_name=account_name,
        ):
            public_access = getattr(container, "public_access", None)
            if public_access and hasattr(public_access, "value"):
                public_access = public_access.value

            lease_state = getattr(container, "lease_state", None)
            if lease_state and hasattr(lease_state, "value"):
                lease_state = lease_state.value

            results.append({
                "id": container.id,
                "name": container.name,
                "resource_group": _extract_resource_group(container.id or ""),
                "public_access": public_access,
                "lease_state": lease_state,
                "has_immutability_policy": getattr(container, "has_immutability_policy", None),
                "has_legal_hold": getattr(container, "has_legal_hold", None),
                "last_modified": str(container.last_modified_time) if getattr(container, "last_modified_time", None) else None,
            })

        return results

    # =========================================================================
    # ALIAS: MANAGED DISKS
    # =========================================================================

    async def _handle_list_azure_managed_disks(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List managed disks (alias for list_azure_disks)."""
        return await self._handle_list_azure_disks(params)

    # =========================================================================
    # ACCOUNT KEYS
    # =========================================================================

    async def _handle_get_azure_storage_account_keys(
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get storage account access keys.

        Returns key names and permissions (not the actual key values for security).
        """
        resource_group = params["resource_group"]
        account_name = params["account_name"]

        keys_result = await self._storage_client.storage_accounts.list_keys(
            resource_group_name=resource_group,
            account_name=account_name,
        )

        keys: list[dict[str, Any]] = []
        for key in keys_result.keys or []:
            permissions = getattr(key, "permissions", None)
            if permissions and hasattr(permissions, "value"):
                permissions = permissions.value
            keys.append({
                "key_name": key.key_name,
                "permissions": permissions,
                # Intentionally NOT returning key.value for security
            })

        return {
            "account_name": account_name,
            "resource_group": resource_group,
            "keys": keys,
        }

    # =========================================================================
    # STORAGE USAGE
    # =========================================================================

    async def _handle_list_azure_storage_usage(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List storage usage by location."""
        location = params["location"]

        results: list[dict[str, Any]] = []
        async for usage in self._storage_client.usages.list_by_location(location):
            results.append({
                "name": usage.name.value if usage.name else None,
                "display_name": usage.name.localized_value if usage.name else None,
                "current_value": usage.current_value,
                "limit": usage.limit,
                "unit": str(usage.unit) if usage.unit else None,
            })

        return results
