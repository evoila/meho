# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Azure Network handler mixin (Phase 92).

Handlers for Azure networking operations: VNets, subnets, NSGs,
load balancers, and public IPs. Uses native async Azure SDK clients.
"""

from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.azure.helpers import (
    _extract_resource_group,
    _safe_tags,
)
from meho_app.modules.connectors.azure.serializers import (
    serialize_azure_load_balancer,
    serialize_azure_nsg,
    serialize_azure_subnet,
    serialize_azure_vnet,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.azure.connector import AzureConnector

logger = get_logger(__name__)


class NetworkHandlerMixin:
    """Mixin providing Azure Network operation handlers.

    Covers VNets, subnets, NSGs (with both security_rules and
    default_security_rules), load balancers, and public IPs.
    All methods use native async Azure SDK calls.
    """

    if TYPE_CHECKING:
        _network_client: Any
        _subscription_id: str
        _resource_group_filter: str | None

    # =========================================================================
    # VNET OPERATIONS
    # =========================================================================

    async def _handle_list_azure_vnets(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List virtual networks.

        If resource_group is provided, lists VNets in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for vnet in self._network_client.virtual_networks.list(resource_group):
                results.append(serialize_azure_vnet(vnet))
        else:
            async for vnet in self._network_client.virtual_networks.list_all():
                results.append(serialize_azure_vnet(vnet))

        return results

    async def _handle_get_azure_vnet(
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get VNet details with subnets."""
        resource_group = params["resource_group"]
        vnet_name = params["vnet_name"]

        vnet = await self._network_client.virtual_networks.get(
            resource_group_name=resource_group,
            virtual_network_name=vnet_name,
        )
        result = serialize_azure_vnet(vnet)

        # Also serialize individual subnets for detail view
        if vnet.subnets:
            result["subnets"] = [serialize_azure_subnet(s) for s in vnet.subnets]

        return result

    # =========================================================================
    # SUBNET OPERATIONS
    # =========================================================================

    async def _handle_list_azure_subnets(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List subnets in a VNet."""
        resource_group = params["resource_group"]
        vnet_name = params["vnet_name"]

        results: list[dict[str, Any]] = []
        async for subnet in self._network_client.subnets.list(
            resource_group_name=resource_group,
            virtual_network_name=vnet_name,
        ):
            results.append(serialize_azure_subnet(subnet))

        return results

    # =========================================================================
    # NSG OPERATIONS
    # =========================================================================

    async def _handle_list_azure_nsgs(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List network security groups.

        If resource_group is provided, lists NSGs in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        Includes both security_rules and default_security_rules.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for nsg in self._network_client.network_security_groups.list(resource_group):
                results.append(serialize_azure_nsg(nsg))
        else:
            async for nsg in self._network_client.network_security_groups.list_all():
                results.append(serialize_azure_nsg(nsg))

        return results

    async def _handle_get_azure_nsg(
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get NSG with security rules (both custom and default)."""
        resource_group = params["resource_group"]
        nsg_name = params["nsg_name"]

        nsg = await self._network_client.network_security_groups.get(
            resource_group_name=resource_group,
            network_security_group_name=nsg_name,
        )
        return serialize_azure_nsg(nsg)

    # =========================================================================
    # LOAD BALANCER OPERATIONS
    # =========================================================================

    async def _handle_list_azure_load_balancers(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List load balancers.

        If resource_group is provided, lists LBs in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for lb in self._network_client.load_balancers.list(resource_group):
                results.append(serialize_azure_load_balancer(lb))
        else:
            async for lb in self._network_client.load_balancers.list_all():
                results.append(serialize_azure_load_balancer(lb))

        return results

    async def _handle_get_azure_load_balancer(
        self: "AzureConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get load balancer details."""
        resource_group = params["resource_group"]
        lb_name = params["lb_name"]

        lb = await self._network_client.load_balancers.get(
            resource_group_name=resource_group,
            load_balancer_name=lb_name,
        )
        return serialize_azure_load_balancer(lb)

    # =========================================================================
    # PUBLIC IP OPERATIONS
    # =========================================================================

    async def _handle_list_azure_public_ips(
        self: "AzureConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List public IP addresses.

        If resource_group is provided, lists public IPs in that group.
        Otherwise falls back to resource_group_filter, then lists all.
        """
        resource_group = params.get("resource_group") or self._resource_group_filter

        results: list[dict[str, Any]] = []

        if resource_group:
            async for pip in self._network_client.public_ip_addresses.list(resource_group):
                results.append(self._serialize_public_ip(pip))
        else:
            async for pip in self._network_client.public_ip_addresses.list_all():
                results.append(self._serialize_public_ip(pip))

        return results

    @staticmethod
    def _serialize_public_ip(pip: Any) -> dict[str, Any]:
        """Serialize a public IP address to a dictionary."""
        sku_name = None
        if pip.sku:
            sku_name = pip.sku.name

        allocation_method = getattr(pip, "public_ip_allocation_method", None)
        if allocation_method and hasattr(allocation_method, "value"):
            allocation_method = allocation_method.value

        ip_version = getattr(pip, "public_ip_address_version", None)
        if ip_version and hasattr(ip_version, "value"):
            ip_version = ip_version.value

        return {
            "id": pip.id,
            "name": pip.name,
            "location": pip.location,
            "resource_group": _extract_resource_group(pip.id or ""),
            "ip_address": getattr(pip, "ip_address", None),
            "allocation_method": allocation_method,
            "ip_version": ip_version,
            "sku_name": sku_name,
            "provisioning_state": pip.provisioning_state,
            "tags": _safe_tags(pip.tags),
        }
