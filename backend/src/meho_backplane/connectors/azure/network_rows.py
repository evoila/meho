# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How the Azure network operations build their rows from Azure's answers.

Every row is built from a fixed list of fields. Nothing else from Azure's
answer is passed on, so a field MEHO does not name (for example the service
key or the authorization keys of an ExpressRoute circuit) never reaches a
result. The functions here only read the JSON Azure sent; the calls are in
:mod:`meho_backplane.connectors.azure.network_reads`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from meho_backplane.connectors.azure._errors import clean_azure_text

__all__ = [
    "Row",
    "express_route_row",
    "gateway_row",
    "load_balancer_row",
    "nsg_detail",
    "nsg_row",
    "private_zone_row",
    "public_ip_row",
    "route_table_row",
    "vnet_detail",
    "vnet_row",
]

Row = dict[str, Any]

#: Inbound rules first, then outbound, like the Azure portal shows them.
_DIRECTION_ORDER: Final[dict[str, int]] = {"inbound": 0, "outbound": 1}

#: Sorts a rule without a priority after every real priority (Azure's highest is 65500).
_NO_PRIORITY: Final[int] = 1_000_000


# ----------------------------------------------------------------------
# Reading values from Azure's answer
# ----------------------------------------------------------------------


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | int | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _props(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``properties`` object Azure puts most fields of a resource in."""
    return _mapping(item.get("properties"))


def _ref_id(value: object) -> str | None:
    """The ``id`` of a reference such as ``{"id": "/subscriptions/..."}``."""
    return _text(_mapping(value).get("id"))


def _ref_ids(value: object) -> list[str]:
    return [ref for ref in (_ref_id(item) for item in _items(value)) if ref]


def _one_or_many(single: object, many: object) -> list[str]:
    """Azure fills either the single field (``sourcePortRange``) or the list (``...Ranges``)."""
    values = _texts(many)
    if values:
        return values
    one = _text(single)
    return [one] if one else []


def _prefixes(address_space: object) -> list[str]:
    return _texts(_mapping(address_space).get("addressPrefixes"))


def _resource_group_of(resource_id: str | None) -> str | None:
    """Read the resource group from an Azure resource ID (``.../resourceGroups/<name>/...``)."""
    if resource_id is None:
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[index + 1] or None
    return None


def _owner(child_id: str | None) -> tuple[str | None, str | None]:
    """Return the ID and the type of the resource a child ID belongs to.

    ``.../providers/Microsoft.Network/networkInterfaces/nic-1/ipConfigurations/ip-1``
    belongs to ``.../providers/Microsoft.Network/networkInterfaces/nic-1``, type
    ``Microsoft.Network/networkInterfaces``. An ID of another shape is
    returned as it is, with no type.
    """
    if child_id is None:
        return None, None
    parts = child_id.split("/")
    marks = [index for index, part in enumerate(parts) if part.lower() == "providers"]
    if not marks or len(parts) < marks[-1] + 4:
        return child_id, None
    start = marks[-1]
    return "/".join(parts[: start + 4]), f"{parts[start + 1]}/{parts[start + 2]}"


def _network_of(subnet_id: str) -> str | None:
    """The ID of the virtual network a subnet ID belongs to."""
    parts = subnet_id.split("/")
    for index in range(len(parts) - 2, 0, -1):
        if parts[index].lower() == "subnets":
            return "/".join(parts[:index]) or None
    return None


def _identity(item: Mapping[str, Any]) -> Row:
    """The four fields every row starts with."""
    resource_id = _text(item.get("id"))
    return {
        "name": _text(item.get("name")),
        "resource_group": _resource_group_of(resource_id),
        "location": _text(item.get("location")),
        "id": resource_id,
    }


def _state(props: Mapping[str, Any]) -> str | None:
    return _text(props.get("provisioningState"))


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


def vnet_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    return {
        **_identity(item),
        "address_prefixes": _prefixes(props.get("addressSpace")),
        "dns_servers": _texts(_mapping(props.get("dhcpOptions")).get("dnsServers")),
        "subnet_count": len(_items(props.get("subnets"))),
        "peering_count": len(_items(props.get("virtualNetworkPeerings"))),
        "provisioning_state": _state(props),
    }


def _subnet_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    delegations = (
        _text(_props(entry).get("serviceName")) for entry in _items(props.get("delegations"))
    )
    return {
        "name": _text(item.get("name")),
        "address_prefixes": _one_or_many(props.get("addressPrefix"), props.get("addressPrefixes")),
        "network_security_group_id": _ref_id(props.get("networkSecurityGroup")),
        "route_table_id": _ref_id(props.get("routeTable")),
        "nat_gateway_id": _ref_id(props.get("natGateway")),
        "delegations": [service for service in delegations if service],
    }


def _peering_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "remote_virtual_network_id": _ref_id(props.get("remoteVirtualNetwork")),
        "peering_state": _text(props.get("peeringState")),
        "peering_sync_level": _text(props.get("peeringSyncLevel")),
        "allow_virtual_network_access": _flag(props.get("allowVirtualNetworkAccess")),
        "allow_forwarded_traffic": _flag(props.get("allowForwardedTraffic")),
        "allow_gateway_transit": _flag(props.get("allowGatewayTransit")),
        "use_remote_gateways": _flag(props.get("useRemoteGateways")),
        "remote_address_prefixes": (
            _prefixes(props.get("remoteAddressSpace"))
            or _prefixes(props.get("remoteVirtualNetworkAddressSpace"))
        ),
    }


def vnet_detail(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    subnets = [_subnet_row(subnet) for subnet in _items(props.get("subnets"))]
    peerings = [_peering_row(peering) for peering in _items(props.get("virtualNetworkPeerings"))]
    return {
        **_identity(item),
        "provisioning_state": _state(props),
        "address_prefixes": _prefixes(props.get("addressSpace")),
        "dns_servers": _texts(_mapping(props.get("dhcpOptions")).get("dnsServers")),
        "subnet_count": len(subnets),
        "peering_count": len(peerings),
        "subnets": subnets,
        "peerings": peerings,
    }


def nsg_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    return {
        **_identity(item),
        "custom_rule_count": len(_items(props.get("securityRules"))),
        "subnet_count": len(_items(props.get("subnets"))),
        "network_interface_count": len(_items(props.get("networkInterfaces"))),
        "provisioning_state": _state(props),
    }


def _rule_row(item: Mapping[str, Any], kind: str) -> Row:
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "kind": kind,
        "direction": _text(props.get("direction")),
        "priority": _int(props.get("priority")),
        "access": _text(props.get("access")),
        "protocol": _text(props.get("protocol")),
        "source_prefixes": _one_or_many(
            props.get("sourceAddressPrefix"), props.get("sourceAddressPrefixes")
        ),
        "source_ports": _one_or_many(props.get("sourcePortRange"), props.get("sourcePortRanges")),
        "destination_prefixes": _one_or_many(
            props.get("destinationAddressPrefix"), props.get("destinationAddressPrefixes")
        ),
        "destination_ports": _one_or_many(
            props.get("destinationPortRange"), props.get("destinationPortRanges")
        ),
        "source_application_security_group_ids": _ref_ids(
            props.get("sourceApplicationSecurityGroups")
        ),
        "destination_application_security_group_ids": _ref_ids(
            props.get("destinationApplicationSecurityGroups")
        ),
        "description": clean_azure_text(props.get("description")),
    }


def _rule_order(rule: Row) -> tuple[int, int, str]:
    direction = rule["direction"]
    priority = rule["priority"]
    return (
        _DIRECTION_ORDER.get(direction.lower(), 2) if direction else 2,
        priority if priority is not None else _NO_PRIORITY,
        rule["name"] or "",
    )


def nsg_detail(item: Mapping[str, Any]) -> Row:
    """The group as one object, and every rule as a row under ``value``."""
    props = _props(item)
    custom = [_rule_row(rule, "custom") for rule in _items(props.get("securityRules"))]
    default = [_rule_row(rule, "default") for rule in _items(props.get("defaultSecurityRules"))]
    group = {
        **_identity(item),
        "custom_rule_count": len(custom),
        "default_rule_count": len(default),
        "subnet_count": len(_items(props.get("subnets"))),
        "network_interface_count": len(_items(props.get("networkInterfaces"))),
        "subnet_ids": _ref_ids(props.get("subnets")),
        "provisioning_state": _state(props),
    }
    return {"network_security_group": group, "value": sorted(custom + default, key=_rule_order)}


def load_balancer_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    sku = _mapping(item.get("sku"))
    frontends = [_props(frontend) for frontend in _items(props.get("frontendIPConfigurations"))]
    public_ips = (_ref_id(frontend.get("publicIPAddress")) for frontend in frontends)
    private_ips = (_text(frontend.get("privateIPAddress")) for frontend in frontends)
    return {
        **_identity(item),
        "sku": _text(sku.get("name")),
        "sku_tier": _text(sku.get("tier")),
        "frontend_count": len(frontends),
        "public_ip_address_ids": [ref for ref in public_ips if ref],
        "private_ip_addresses": [address for address in private_ips if address],
        "backend_pool_count": len(_items(props.get("backendAddressPools"))),
        "rule_count": len(_items(props.get("loadBalancingRules"))),
        "probe_count": len(_items(props.get("probes"))),
        "inbound_nat_rule_count": len(_items(props.get("inboundNatRules"))),
        "outbound_rule_count": len(_items(props.get("outboundRules"))),
        "provisioning_state": _state(props),
    }


def public_ip_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    sku = _mapping(item.get("sku"))
    used_by = _ref_id(props.get("ipConfiguration")) or _ref_id(props.get("natGateway"))
    attached_to, attached_type = _owner(used_by)
    return {
        **_identity(item),
        "ip_address": _text(props.get("ipAddress")),
        "ip_version": _text(props.get("publicIPAddressVersion")),
        "allocation": _text(props.get("publicIPAllocationMethod")),
        "sku": _text(sku.get("name")),
        "sku_tier": _text(sku.get("tier")),
        "zones": _texts(item.get("zones")),
        "attached_to": attached_to,
        "attached_type": attached_type,
        "dns_fqdn": _text(_mapping(props.get("dnsSettings")).get("fqdn")),
        "provisioning_state": _state(props),
    }


def gateway_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    configs = [_props(config) for config in _items(props.get("ipConfigurations"))]
    subnets = [ref for ref in (_ref_id(config.get("subnet")) for config in configs) if ref]
    public_ips = (_ref_id(config.get("publicIPAddress")) for config in configs)
    return {
        **_identity(item),
        "gateway_type": _text(props.get("gatewayType")),
        "vpn_type": _text(props.get("vpnType")),
        "sku": _text(_mapping(props.get("sku")).get("name")),
        "generation": _text(props.get("vpnGatewayGeneration")),
        "bgp_enabled": _flag(props.get("enableBgp")),
        "bgp_asn": _int(_mapping(props.get("bgpSettings")).get("asn")),
        "active_active": _flag(props.get("activeActive")),
        "virtual_network_id": _network_of(subnets[0]) if subnets else None,
        "public_ip_address_ids": [ref for ref in public_ips if ref],
        "provisioning_state": _state(props),
    }


def express_route_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    provider = _mapping(props.get("serviceProviderProperties"))
    sku = _mapping(item.get("sku"))
    return {
        **_identity(item),
        "provider": _text(provider.get("serviceProviderName")),
        "peering_location": _text(provider.get("peeringLocation")),
        "bandwidth_mbps": _int(provider.get("bandwidthInMbps")),
        "bandwidth_gbps": _number(props.get("bandwidthInGbps")),
        "sku_tier": _text(sku.get("tier")),
        "sku_family": _text(sku.get("family")),
        "circuit_state": _text(props.get("circuitProvisioningState")),
        "provider_state": _text(props.get("serviceProviderProvisioningState")),
        "global_reach_enabled": _flag(props.get("globalReachEnabled")),
        "peering_count": len(_items(props.get("peerings"))),
        "provisioning_state": _state(props),
    }


def _route_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    return {
        "name": _text(item.get("name")),
        "address_prefix": _text(props.get("addressPrefix")),
        "next_hop_type": _text(props.get("nextHopType")),
        "next_hop_ip_address": _text(props.get("nextHopIpAddress")),
    }


def route_table_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    routes = [_route_row(route) for route in _items(props.get("routes"))]
    return {
        **_identity(item),
        "bgp_route_propagation_disabled": _flag(props.get("disableBgpRoutePropagation")),
        "subnet_count": len(_items(props.get("subnets"))),
        "route_count": len(routes),
        "routes": routes,
        "provisioning_state": _state(props),
    }


def private_zone_row(item: Mapping[str, Any]) -> Row:
    props = _props(item)
    return {
        **_identity(item),
        "record_set_count": _int(props.get("numberOfRecordSets")),
        "virtual_network_link_count": _int(props.get("numberOfVirtualNetworkLinks")),
        "registration_link_count": _int(props.get("numberOfVirtualNetworkLinksWithRegistration")),
        "provisioning_state": _state(props),
    }
