# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-written Azure Resource Manager paths and API versions.

Every path the Azure connector calls is a constant in this module, so one CI
check can compare each of them with Azure's own API description files
(``backend/tests/test_connectors_azure_spec_reconcile.py``). A typo, a
renamed endpoint or a wrong API version then fails in CI and not on a
customer system. Later reads add their paths here, and each new ``*_PATH``
constant gets one line in that check.

Azure pins an API version per call (the ``api-version`` query parameter). The
versions are kept next to their paths, one per family of operations.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "LOCATIONS_PATH",
    "RESOURCE_GRAPH_API_VERSION",
    "RESOURCE_GRAPH_PATH",
    "SUBSCRIPTIONS_API_VERSION",
    "SUBSCRIPTION_PATH",
    "locations_path",
    "subscription_path",
]

#: ``GET /subscriptions/{subscriptionId}``: one subscription (Subscriptions - Get).
SUBSCRIPTION_PATH: Final[str] = "/subscriptions/{subscription_id}"

#: ``GET /subscriptions/{subscriptionId}/locations``: the Azure regions of one
#: subscription (Subscriptions - List Locations).
LOCATIONS_PATH: Final[str] = "/subscriptions/{subscription_id}/locations"

#: The API version of the subscription and location reads (Microsoft.Resources).
SUBSCRIPTIONS_API_VERSION: Final[str] = "2022-12-01"

#: ``POST /providers/Microsoft.ResourceGraph/resources``: one Resource Graph
#: query (Resources - Resources). A read, even though it is a POST.
RESOURCE_GRAPH_PATH: Final[str] = "/providers/Microsoft.ResourceGraph/resources"

#: The API version of Resource Graph queries (Microsoft.ResourceGraph).
RESOURCE_GRAPH_API_VERSION: Final[str] = "2024-04-01"


def subscription_path(subscription_id: str) -> str:
    """Return the path of one subscription.

    *subscription_id* must already be checked as a GUID (see
    :func:`~meho_backplane.connectors.azure.session.resolve_target_config`), so
    it cannot change the path.
    """
    return SUBSCRIPTION_PATH.format(subscription_id=subscription_id)


def locations_path(subscription_id: str) -> str:
    """Return the path of the regions of one subscription.

    *subscription_id* must already be checked as a GUID, like in
    :func:`subscription_path`.
    """
    return LOCATIONS_PATH.format(subscription_id=subscription_id)


# ---- network (Task #5085) ----

__all__ += [
    "EXPRESS_ROUTE_CIRCUITS_PATH",
    "LOAD_BALANCERS_PATH",
    "NETWORK_API_VERSION",
    "NETWORK_SECURITY_GROUPS_PATH",
    "NETWORK_SECURITY_GROUP_PATH",
    "PRIVATE_DNS_API_VERSION",
    "PRIVATE_DNS_ZONES_PATH",
    "PUBLIC_IP_ADDRESSES_PATH",
    "ROUTE_TABLES_PATH",
    "VIRTUAL_NETWORKS_PATH",
    "VIRTUAL_NETWORK_GATEWAYS_PATH",
    "VIRTUAL_NETWORK_PATH",
]

#: The API version of the Microsoft.Network reads: virtual networks, network
#: security groups, load balancers, public IP addresses, gateways, ExpressRoute
#: circuits and route tables.
NETWORK_API_VERSION: Final[str] = "2026-01-01"

#: ``GET .../providers/Microsoft.Network/virtualNetworks``: every virtual
#: network of the subscription (Virtual Networks - List All).
VIRTUAL_NETWORKS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/virtualNetworks"
)

#: ``GET .../resourceGroups/{rg}/providers/Microsoft.Network/virtualNetworks/{name}``:
#: one virtual network with its subnets and peerings (Virtual Networks - Get).
VIRTUAL_NETWORK_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Network/virtualNetworks/{name}"
)

#: ``GET .../providers/Microsoft.Network/networkSecurityGroups``: every network
#: security group of the subscription (Network Security Groups - List All).
NETWORK_SECURITY_GROUPS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/networkSecurityGroups"
)

#: ``GET .../resourceGroups/{rg}/providers/Microsoft.Network/networkSecurityGroups/{name}``:
#: one network security group with all its rules (Network Security Groups - Get).
NETWORK_SECURITY_GROUP_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Network/networkSecurityGroups/{name}"
)

#: ``GET .../providers/Microsoft.Network/loadBalancers``: every load balancer of
#: the subscription (Load Balancers - List All).
LOAD_BALANCERS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/loadBalancers"
)

#: ``GET .../providers/Microsoft.Network/publicIPAddresses``: every public IP
#: address of the subscription (Public IP Addresses - List All).
PUBLIC_IP_ADDRESSES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/publicIPAddresses"
)

#: ``GET .../resourceGroups/{rg}/providers/Microsoft.Network/virtualNetworkGateways``:
#: the VPN and ExpressRoute gateways of one resource group (Virtual Network
#: Gateways - List). Azure has no list of gateways per subscription.
VIRTUAL_NETWORK_GATEWAYS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
    "/providers/Microsoft.Network/virtualNetworkGateways"
)

#: ``GET .../providers/Microsoft.Network/expressRouteCircuits``: every
#: ExpressRoute circuit of the subscription (Express Route Circuits - List All).
EXPRESS_ROUTE_CIRCUITS_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/expressRouteCircuits"
)

#: ``GET .../providers/Microsoft.Network/routeTables``: every route table of the
#: subscription, with its routes (Route Tables - List All).
ROUTE_TABLES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/routeTables"
)

#: The API version of the private DNS zone reads (Microsoft.Network/privateDnsZones).
PRIVATE_DNS_API_VERSION: Final[str] = "2024-06-01"

#: ``GET .../providers/Microsoft.Network/privateDnsZones``: every private DNS
#: zone of the subscription (Private Zones - List).
PRIVATE_DNS_ZONES_PATH: Final[str] = (
    "/subscriptions/{subscription_id}/providers/Microsoft.Network/privateDnsZones"
)
