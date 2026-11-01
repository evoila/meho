# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Azure network operations.

The operation table is in :mod:`meho_backplane.connectors.azure.network`.
The schemas tell the AI which fields it may send and what comes back. The
name patterns here are also the checks the operation bodies run before a
name goes into a URL path (:mod:`.network_reads`).
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.azure._model import TEXT_OR_NULL

__all__ = [
    "EXPRESS_ROUTE_LIST_RESPONSE",
    "GATEWAY_LIST_PARAMS",
    "GATEWAY_LIST_RESPONSE",
    "LOAD_BALANCER_LIST_RESPONSE",
    "NSG_LIST_RESPONSE",
    "NSG_SHOW_PARAMS",
    "NSG_SHOW_RESPONSE",
    "PRIVATE_DNS_LIST_RESPONSE",
    "PUBLIC_IP_LIST_RESPONSE",
    "RESOURCE_GROUP_CHARS",
    "RESOURCE_GROUP_MAX_LENGTH",
    "RESOURCE_NAME_CHARS",
    "ROUTE_TABLE_LIST_RESPONSE",
    "SECURITY_GROUP_NAME_MAX_LENGTH",
    "VIRTUAL_NETWORK_NAME_MAX_LENGTH",
    "VNET_LIST_RESPONSE",
    "VNET_SHOW_PARAMS",
    "VNET_SHOW_RESPONSE",
]

#: A resource group name: letters, digits, ``_``, ``-``, ``.``, ``(`` and
#: ``)``, 1 to 90 characters, not ending with ``.`` (Microsoft's naming rules).
#: Not ending with ``.`` also rules out ``.`` and ``..``, which could move the
#: URL path to another subscription.
RESOURCE_GROUP_CHARS: Final[str] = r"[-\w.()]{0,89}[-\w()]"
RESOURCE_GROUP_MAX_LENGTH: Final[int] = 90

#: A network resource name: letters, digits, ``_``, ``.`` and ``-``; it starts
#: and ends with a letter, a digit or ``_``.
RESOURCE_NAME_CHARS: Final[str] = r"\w(?:[\w.-]*\w)?"
VIRTUAL_NETWORK_NAME_MAX_LENGTH: Final[int] = 64
SECURITY_GROUP_NAME_MAX_LENGTH: Final[int] = 80

#: ``(?!\n)``: the validator uses Python's ``re``, where ``$`` also matches
#: before a final newline, so without it ``"name\n"`` would pass.
_RESOURCE_GROUP_PATTERN: Final[str] = f"^{RESOURCE_GROUP_CHARS}$(?!\\n)"
_RESOURCE_NAME_PATTERN: Final[str] = f"^{RESOURCE_NAME_CHARS}$(?!\\n)"

_EXAMPLE_GROUP: Final[str] = "rg-network-example"


def _text(description: str) -> dict[str, Any]:
    return {"type": TEXT_OR_NULL, "description": description}


def _texts(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _count(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def _int(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _number(description: str) -> dict[str, Any]:
    return {"type": ["number", "null"], "description": description}


def _flag(description: str) -> dict[str, Any]:
    return {"type": ["boolean", "null"], "description": description}


def _row(fields: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": ["name"]}


def _identity(what: str) -> dict[str, Any]:
    """The four fields every row starts with."""
    return {
        "name": _text(f"The name of the {what}."),
        "resource_group": _text("The resource group, read from the Azure resource ID."),
        "location": _text("The Azure region, for example westeurope."),
        "id": _text(f"The Azure resource ID of the {what}."),
    }


_PROVISIONING_STATE: Final[dict[str, Any]] = _text(
    "Succeeded, Failed, Updating, Deleting or another provisioning state from Azure."
)


def _paged_list(row: dict[str, Any], what: str) -> dict[str, Any]:
    """The answer of a list: the rows under ``value`` and how the paging went."""
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": row,
                "description": (
                    f"One row per {what}. More than 50 rows (or more than 4 KB) come "
                    "back as a link to the stored result; read it with result_query."
                ),
            },
            "truncated": {
                "type": "boolean",
                "description": "True when MEHO stopped reading pages at one of its limits.",
            },
            "omission": _text("What was left out and how to narrow the request, when truncated."),
            "omission_kind": _text(
                "Which limit stopped the list: row_limit, page_limit or time_limit."
            ),
            "pages_read": _count("How many pages MEHO read from Azure."),
        },
        "required": ["value", "truncated", "pages_read"],
    }


# ----------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------

_RESOURCE_GROUP_PARAM: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": RESOURCE_GROUP_MAX_LENGTH,
    "pattern": _RESOURCE_GROUP_PATTERN,
    "description": (
        "The resource group name, as Azure shows it. Letters, digits, '_', '-', '.', "
        "'(' and ')'; it does not end with '.'."
    ),
}


def _name_param(what: str, min_length: int, max_length: int) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": min_length,
        "maxLength": max_length,
        "pattern": _RESOURCE_NAME_PATTERN,
        "description": (
            f"The name of the {what}, as Azure shows it. Letters, digits, '_', '.' and "
            "'-'; it starts and ends with a letter, a digit or '_'."
        ),
    }


VNET_SHOW_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP_PARAM,
        "name": _name_param("virtual network", 2, VIRTUAL_NETWORK_NAME_MAX_LENGTH),
    },
    "required": ["resource_group", "name"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_GROUP, "name": "vnet-hub-example"}],
}

NSG_SHOW_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "resource_group": _RESOURCE_GROUP_PARAM,
        "name": _name_param("network security group", 1, SECURITY_GROUP_NAME_MAX_LENGTH),
    },
    "required": ["resource_group", "name"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_GROUP, "name": "nsg-web-example"}],
}

GATEWAY_LIST_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"resource_group": _RESOURCE_GROUP_PARAM},
    "required": ["resource_group"],
    "additionalProperties": False,
    "examples": [{"resource_group": _EXAMPLE_GROUP}],
}


# ----------------------------------------------------------------------
# Virtual networks
# ----------------------------------------------------------------------

_ADDRESS_PREFIXES: Final[dict[str, Any]] = _texts(
    "The address ranges of the network, in CIDR notation (for example 198.51.100.0/24)."
)
_DNS_SERVERS: Final[dict[str, Any]] = _texts(
    "The DNS servers the network gives to its machines. Empty means Azure's own DNS."
)

VNET_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("virtual network"),
            "address_prefixes": _ADDRESS_PREFIXES,
            "dns_servers": _DNS_SERVERS,
            "subnet_count": _count("How many subnets the network has."),
            "peering_count": _count("How many peerings (links to other networks) it has."),
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "virtual network",
)

_SUBNET_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The subnet name. GatewaySubnet holds a VPN or ExpressRoute gateway."),
        "address_prefixes": _texts("The address ranges of the subnet, in CIDR notation."),
        "network_security_group_id": _text(
            "The ID of the network security group on the subnet, or null for none."
        ),
        "route_table_id": _text("The ID of the route table on the subnet, or null for none."),
        "nat_gateway_id": _text("The ID of the NAT gateway for outbound traffic, or null."),
        "delegations": _texts(
            "The Azure services the subnet is handed to, such as Microsoft.Sql/managedInstances."
        ),
    }
)

_PEERING_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The peering name."),
        "remote_virtual_network_id": _text("The ID of the network on the other side."),
        "peering_state": _text("Connected, Initiated or Disconnected."),
        "peering_sync_level": _text(
            "FullyInSync, or which side has an address range the other side does not know."
        ),
        "allow_virtual_network_access": _flag("Machines on both sides may reach each other."),
        "allow_forwarded_traffic": _flag("Traffic forwarded by the other side is let in."),
        "allow_gateway_transit": _flag("The other side may use this network's gateway."),
        "use_remote_gateways": _flag("This network uses the gateway of the other side."),
        "remote_address_prefixes": _texts("The address ranges of the network on the other side."),
    }
)

VNET_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        **_identity("virtual network"),
        "provisioning_state": _PROVISIONING_STATE,
        "address_prefixes": _ADDRESS_PREFIXES,
        "dns_servers": _DNS_SERVERS,
        "subnet_count": _count("How many subnets the network has."),
        "peering_count": _count("How many peerings the network has."),
        "subnets": {"type": "array", "items": _SUBNET_ROW, "description": "Every subnet."},
        "peerings": {"type": "array", "items": _PEERING_ROW, "description": "Every peering."},
    },
    "required": ["name", "subnets", "peerings"],
}


# ----------------------------------------------------------------------
# Network security groups
# ----------------------------------------------------------------------

_NSG_COUNTS: Final[dict[str, Any]] = {
    "custom_rule_count": _count("How many rules people added."),
    "subnet_count": _count("How many subnets use the group."),
    "network_interface_count": _count("How many network cards (of VMs) use the group."),
}

NSG_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("network security group"),
            **_NSG_COUNTS,
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "network security group",
)

_RULE_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The rule name."),
        "kind": _text("custom (added by people) or default (Azure's own rules, priority 65000+)."),
        "direction": _text("Inbound or Outbound."),
        "priority": _int("Lower numbers are checked first; the first rule that matches wins."),
        "access": _text("Allow or Deny."),
        "protocol": _text("Tcp, Udp, Icmp, Esp, Ah, or * for every protocol."),
        "source_prefixes": _texts(
            "Where traffic comes from: CIDR ranges, * for any, or service tags such as Internet."
        ),
        "source_ports": _texts("Source ports or port ranges; * for every port."),
        "destination_prefixes": _texts("Where traffic goes: CIDR ranges, * or service tags."),
        "destination_ports": _texts("Destination ports or port ranges; * for every port."),
        "source_application_security_group_ids": _texts(
            "IDs of application security groups (named groups of VM network cards) as source."
        ),
        "destination_application_security_group_ids": _texts(
            "IDs of application security groups as destination."
        ),
        "description": _text("The rule's description, text from Azure."),
    }
)

NSG_SHOW_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "network_security_group": {
            "type": "object",
            "description": (
                "The group itself. It stays next to the rules when the rules come back "
                "as a link to the stored result."
            ),
            "properties": {
                **_identity("network security group"),
                **_NSG_COUNTS,
                "default_rule_count": _count("How many default rules Azure added (usually 6)."),
                "subnet_ids": _texts("The IDs of the subnets that use the group."),
                "provisioning_state": _PROVISIONING_STATE,
            },
        },
        "value": {
            "type": "array",
            "items": _RULE_ROW,
            "description": (
                "Every rule, custom and default, sorted by direction and priority. More "
                "than 50 rules come back as a link to the stored result."
            ),
        },
    },
    "required": ["network_security_group", "value"],
}


# ----------------------------------------------------------------------
# Load balancers, public IP addresses, gateways, circuits, routes, DNS
# ----------------------------------------------------------------------

LOAD_BALANCER_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("load balancer"),
            "sku": _text("Basic, Standard, Gateway or Service. Azure retired Basic in 2025."),
            "sku_tier": _text("Regional or Global."),
            "frontend_count": _count("How many front-end IP configurations it has."),
            "public_ip_address_ids": _texts("IDs of the public IP addresses on the front end."),
            "private_ip_addresses": _texts("Private IP addresses on the front end."),
            "backend_pool_count": _count("How many back-end pools it has."),
            "rule_count": _count("How many load-balancing rules it has."),
            "probe_count": _count("How many health probes it has."),
            "inbound_nat_rule_count": _count("How many inbound NAT rules it has."),
            "outbound_rule_count": _count("How many outbound rules it has."),
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "load balancer",
)

PUBLIC_IP_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("public IP address"),
            "ip_address": _text("The address, or null while Azure has not given one."),
            "ip_version": _text("IPv4 or IPv6."),
            "allocation": _text("Static or Dynamic."),
            "sku": _text("Basic, Standard or StandardV2. Azure retired Basic in 2025."),
            "sku_tier": _text("Regional or Global."),
            "zones": _texts("The availability zones of the address."),
            "attached_to": _text(
                "The ID of the resource that uses the address (a network card, load "
                "balancer, gateway, NAT gateway, ...), or null when nothing uses it."
            ),
            "attached_type": _text(
                "The Azure type of that resource, for example Microsoft.Network/networkInterfaces."
            ),
            "dns_fqdn": _text("The DNS name of the address, if one is set."),
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "public IP address",
)

GATEWAY_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("gateway"),
            "gateway_type": _text("Vpn, ExpressRoute or LocalGateway."),
            "vpn_type": _text("RouteBased or PolicyBased (VPN gateways only)."),
            "sku": _text("The size, for example VpnGw2AZ or ErGw1AZ."),
            "generation": _text("Generation1, Generation2 or None (VPN gateways only)."),
            "bgp_enabled": _flag("Whether BGP (dynamic routing) is on."),
            "bgp_asn": _int("The BGP autonomous system number of the gateway."),
            "active_active": _flag("Whether both gateway instances carry traffic."),
            "virtual_network_id": _text("The ID of the network the gateway sits in."),
            "public_ip_address_ids": _texts("IDs of the public IP addresses of the gateway."),
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "gateway",
)

EXPRESS_ROUTE_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("ExpressRoute circuit"),
            "provider": _text("The connectivity provider."),
            "peering_location": _text("Where the circuit meets Microsoft's network."),
            "bandwidth_mbps": _int("The bandwidth in Mbps (circuits through a provider)."),
            "bandwidth_gbps": _number("The bandwidth in Gbps (circuits on an ExpressRoute port)."),
            "sku_tier": _text("Local, Standard, Premium or another tier."),
            "sku_family": _text("MeteredData or UnlimitedData."),
            "circuit_state": _text("Enabled or Disabled: the state on Microsoft's side."),
            "provider_state": _text(
                "NotProvisioned, Provisioning, Provisioned, ...: the state on the provider's side."
            ),
            "global_reach_enabled": _flag("Whether Global Reach links it to other circuits."),
            "peering_count": _count("How many peerings (private, Microsoft) are set up."),
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "ExpressRoute circuit",
)

_ROUTE_ROW: Final[dict[str, Any]] = _row(
    {
        "name": _text("The route name."),
        "address_prefix": _text("The destination range in CIDR notation, or a service tag."),
        "next_hop_type": _text(
            "VirtualNetworkGateway, VnetLocal, Internet, VirtualAppliance or None (drop)."
        ),
        "next_hop_ip_address": _text("The next IP address, for a VirtualAppliance hop."),
    }
)

ROUTE_TABLE_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("route table"),
            "bgp_route_propagation_disabled": _flag(
                "True when routes learned by gateways (BGP) are not used in these subnets."
            ),
            "subnet_count": _count("How many subnets use the table."),
            "route_count": _count("How many routes the table has."),
            "routes": {"type": "array", "items": _ROUTE_ROW, "description": "Every route."},
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "route table",
)

PRIVATE_DNS_LIST_RESPONSE: Final[dict[str, Any]] = _paged_list(
    _row(
        {
            **_identity("private DNS zone"),
            "record_set_count": _int("How many record sets (DNS names) the zone holds."),
            "virtual_network_link_count": _int("How many virtual networks are linked."),
            "registration_link_count": _int(
                "How many linked networks register their machines' names in the zone."
            ),
            "provisioning_state": _PROVISIONING_STATE,
        }
    ),
    "private DNS zone",
)
