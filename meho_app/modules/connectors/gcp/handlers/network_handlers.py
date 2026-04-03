# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
GCP Network Handlers (TASK-102)

Handlers for VPC network, subnetwork, and firewall operations.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.connectors.gcp.helpers import extract_name_from_url
from meho_app.modules.connectors.gcp.serializers import (
    serialize_firewall,
    serialize_network,
    serialize_subnetwork,
)

if TYPE_CHECKING:
    from meho_app.modules.connectors.gcp.connector import GCPConnector

logger = get_logger(__name__)


class NetworkHandlerMixin:
    """Mixin providing network operation handlers."""

    # Type hints for IDE support
    if TYPE_CHECKING:
        _networks_client: Any
        _subnetworks_client: Any
        _firewalls_client: Any
        _credentials: Any
        project_id: str
        default_region: str

    # =========================================================================
    # VPC NETWORK OPERATIONS
    # =========================================================================

    async def _handle_list_networks(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List VPC networks."""
        from google.cloud import compute_v1

        filter_str = params.get("filter")

        request = compute_v1.ListNetworksRequest(
            project=self.project_id,
            filter=filter_str,
        )
        networks = await asyncio.to_thread(
            lambda: list(self._networks_client.list(request=request))
        )

        return [serialize_network(n) for n in networks]

    async def _handle_get_network(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get VPC network details."""
        from google.cloud import compute_v1

        network_name = params["network_name"]

        request = compute_v1.GetNetworkRequest(
            project=self.project_id,
            network=network_name,
        )
        network = await asyncio.to_thread(lambda: self._networks_client.get(request=request))

        return serialize_network(network)

    # =========================================================================
    # SUBNETWORK OPERATIONS
    # =========================================================================

    async def _handle_list_subnetworks(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List subnetworks."""
        from google.cloud import compute_v1

        region = params.get("region")
        filter_str = params.get("filter")

        subnetworks = []

        if region:
            request = compute_v1.ListSubnetworksRequest(
                project=self.project_id,
                region=region,
                filter=filter_str,
            )
            response = await asyncio.to_thread(
                lambda: list(self._subnetworks_client.list(request=request))
            )
            subnetworks.extend(response)
        else:
            # Aggregated list across all regions
            request = compute_v1.AggregatedListSubnetworksRequest(
                project=self.project_id,
                filter=filter_str,
            )
            response = await asyncio.to_thread(
                lambda: self._subnetworks_client.aggregated_list(request=request)
            )
            for _region_name, subnetworks_scoped_list in response:
                if subnetworks_scoped_list.subnetworks:
                    subnetworks.extend(subnetworks_scoped_list.subnetworks)

        return [serialize_subnetwork(s) for s in subnetworks]

    async def _handle_get_subnetwork(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> dict[str, Any]:
        """Get subnetwork details."""
        from google.cloud import compute_v1

        subnetwork_name = params["subnetwork_name"]
        region = params.get("region", self.default_region)

        request = compute_v1.GetSubnetworkRequest(
            project=self.project_id,
            region=region,
            subnetwork=subnetwork_name,
        )
        subnetwork = await asyncio.to_thread(lambda: self._subnetworks_client.get(request=request))

        return serialize_subnetwork(subnetwork)

    # =========================================================================
    # FIREWALL OPERATIONS
    # =========================================================================

    async def _handle_list_firewalls(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List firewall rules."""
        from google.cloud import compute_v1

        filter_str = params.get("filter")

        request = compute_v1.ListFirewallsRequest(
            project=self.project_id,
            filter=filter_str,
        )
        firewalls = await asyncio.to_thread(
            lambda: list(self._firewalls_client.list(request=request))
        )

        return [serialize_firewall(f) for f in firewalls]

    async def _handle_get_firewall(self: "GCPConnector", params: dict[str, Any]) -> dict[str, Any]:  # type: ignore[misc]
        """Get firewall rule details."""
        from google.cloud import compute_v1

        firewall_name = params["firewall_name"]

        request = compute_v1.GetFirewallRequest(
            project=self.project_id,
            firewall=firewall_name,
        )
        firewall = await asyncio.to_thread(lambda: self._firewalls_client.get(request=request))

        return serialize_firewall(firewall)

    # =========================================================================
    # ROUTE OPERATIONS
    # =========================================================================

    async def _handle_list_routes(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List routes."""
        from google.cloud import compute_v1

        filter_str = params.get("filter")

        routes_client = compute_v1.RoutesClient(credentials=self._credentials)

        request = compute_v1.ListRoutesRequest(
            project=self.project_id,
            filter=filter_str,
        )
        routes = await asyncio.to_thread(lambda: list(routes_client.list(request=request)))

        return [
            {
                "id": str(r.id),
                "name": r.name,
                "description": r.description,
                "network": extract_name_from_url(r.network or ""),
                "dest_range": r.dest_range,
                "priority": r.priority,
                "next_hop_gateway": extract_name_from_url(r.next_hop_gateway or ""),
                "next_hop_instance": extract_name_from_url(r.next_hop_instance or ""),
                "next_hop_ip": r.next_hop_ip,
                "next_hop_network": extract_name_from_url(r.next_hop_network or ""),
                "next_hop_peering": r.next_hop_peering,
                "tags": list(r.tags or []),
            }
            for r in routes
        ]

    # =========================================================================
    # ADDRESS OPERATIONS
    # =========================================================================

    async def _handle_list_addresses(  # type: ignore[misc]
        self: "GCPConnector", params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """List static IP addresses."""
        from google.cloud import compute_v1

        region = params.get("region")

        addresses = []

        if region:
            # Regional addresses
            addresses_client = compute_v1.AddressesClient(credentials=self._credentials)
            request = compute_v1.ListAddressesRequest(
                project=self.project_id,
                region=region,
            )
            response = await asyncio.to_thread(lambda: list(addresses_client.list(request=request)))
            addresses.extend(response)
        else:
            # Global addresses
            global_addresses_client = compute_v1.GlobalAddressesClient(
                credentials=self._credentials
            )
            request = compute_v1.ListGlobalAddressesRequest(
                project=self.project_id,
            )
            response = await asyncio.to_thread(
                lambda: list(global_addresses_client.list(request=request))
            )
            addresses.extend(response)

        return [
            {
                "id": str(a.id),
                "name": a.name,
                "description": a.description,
                "address": a.address,
                "address_type": a.address_type,
                "purpose": a.purpose,
                "status": a.status,
                "region": extract_name_from_url(a.region or "") if a.region else "global",
                "users": [extract_name_from_url(u) for u in (a.users or [])],
            }
            for a in addresses
        ]
